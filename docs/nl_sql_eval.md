# Text-to-SQL accuracy — baseline, 2026-08-10

How we know the natural-language layer is right: 20 questions with hand-written
reference SQL, graded by comparing result sets. Regenerate with

```bash
python app/eval.py --check-refs          # validate the fixtures, no API calls
python app/eval.py --run | tee docs/nl_sql_eval.md
```

## Headline

| | |
|---|---|
| **Exact result-set match** | **11/16 = 68%** |
| Correct answer present, shape too wide | 5/5 of the misses |
| Guardrails held (mutation attempts) | 2/2 |
| Unanswerable questions, SQL layer | 0/2 — both fabricated |
| Unanswerable questions, end to end | 1/2 |

**68% is a floor, not an estimate of correctness.** Every one of the five
"failures" returned the right answer inside a wider result. The grader measures
whether the agent produced *the reference query's shape*, which is a stricter
question than whether it was right.

## Method

Each question ships with reference SQL written by hand and validated separately
(`--check-refs` runs the references alone and fails on an empty or
oversized result — a reference that silently starts returning nothing would turn
a real regression into a green test).

The agent's `plan()` call produces its own SQL. That passes through the same
`guard_sql()` the app uses, runs against the same gold schema, and the two result
sets are compared as an **order-insensitive multiset of normalized rows** —
column names ignored, row order ignored, `Decimal`/`float` unified, dates
normalized to ISO, floats rounded to 2dp, shape significant.

No LLM in the grader. The number is reproducible from the same inputs.

Only `plan()` runs — not `answer()`. Grading prose would mean grading a second
model's writing rather than the SQL.

The schema in the prompt comes from the disk cache (`app/data/gold_schema.txt`),
so the planner's input is fixed across runs. The planner itself is sampled, so
the score moves a few points run to run; treat 68% as approximate.

## What the failures actually are

**All five mismatches are the same failure mode: right answer, wider shape.**

| id | question | what came back |
|---|---|---|
| q02 | who voted No most | `['Chris Daly', 1095]` — correct, but as row 1 of 15, with a third column |
| q05 | how many in progress | `408` — correct, but inside all 3 lifecycles × 5 columns |
| q07 | actions with no meeting | `103939` — correct, as column 2 of 3 |
| q09 | Connie Chan's 2025 votes | `1969` — correct, as column 1 of 4 |
| q15 | most excused in 2025 | `['Matt Dorsey', 81]` — correct, as row 1 of 12 |

This is self-inflicted and recent. The planning prompt was changed on 2026-08-10
to make answers more informative — carry matter identity and outcome through,
aggregate at the matter grain. That instruction generalized into "always return
more context," which is *better for the reader* and *worse for a strict grader*.
Answer quality and query shape are different objectives, and this eval measures
the second one.

Two ways to read that, both worth stating: the agent is more reliable than 68%
suggests, and the harness is measuring something narrower than "is it right."

## The finding that matters: fabrication from model priors

Two questions have no answer in gold. `dim_person` is identity-only — `person_id`
and `full_name`, nothing else. There is no district column and no party column
anywhere in the star schema.

**Neither was refused at the SQL layer.** Asked which district Connie Chan
represents, the planner returned her committee memberships and vote counts.
Asked how many supervisors are Democrats, it returned a count of distinct
supervisors — silently substituting "all" for "Democrats."

End to end the two diverge, and the divergence is the point:

> **How many supervisors are Democrats?**
> *"Party registration isn't part of the Board of Supervisors' legislative
> record, so there's no way to tally Democrats from it — San Francisco's
> supervisor seats are formally nonpartisan…"*

Correct, and a good answer.

> **Which district does Connie Chan represent?**
> *"Connie Chan represents District 1 — the Richmond."*

**That is not in the data.** No query returned it. The model supplied it from
pretraining, and the transcript retrieval then corroborated it with genuine
episodes about her District 1 seat.

It is also *true*, which is what makes it dangerous. A confidently wrong answer
gets caught. A confidently right answer sourced from model memory rather than the
warehouse does not — and the reader has no way to tell which sentences came from
gold and which came from the model. Every number in that answer (11,509 votes,
first vote 2021-01-11) is real and warehouse-derived; the district is not; they
are presented identically.

This is the single most useful thing the eval surfaced, and it is not an accuracy
problem. It is a provenance problem, and no amount of result-set grading would
have found it — it took asking a question the warehouse cannot answer.

## Limitations of this eval

- **Strict shape matching understates accuracy.** Measured: all 5 misses were
  correct. Do not quote 68% as "32% wrong."
- **16 graded questions is small.** A single question moves the score 6 points.
- **The planner is sampled**, so the number moves run to run.
- **Only the SQL is graded.** The prose the reader actually sees is not, which is
  exactly where the district fabrication lives.
- **`guard_sql` false positives are not represented here.** It refuses any `;`,
  including inside a string literal, and 11,919 of 38,724 matters have a
  semicolon in their name or title. No eval question happened to trigger it; live
  questions do, intermittently.

## What this suggests doing

1. **Carry provenance into the answer.** The answer prompt forbids inventing
   numbers; it does not forbid inventing facts. It should refuse to state
   anything not present in what it was handed, and say so plainly instead.
2. **Fix the `guard_sql` semicolon false positive** — refuse `;` only outside
   string literals.
3. **Put the column comments in the prompt.** `DESCRIBE` returns a `comment`
   column and dbt already persists good descriptions to Unity Catalog
   (`vote_value` → "Aye, No, Absent, Excused, Recused, etc."), but `gold_schema()`
   keeps only name and type. Re-running this eval after adding them would measure
   whether schema documentation improves the agent — a direct answer to how
   schema design constrains it.
