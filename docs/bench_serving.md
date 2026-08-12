# Serving-path benchmark — 2026-08-12

Should `gold.member_vote_record` stay a view, or become a table — and if a table,
does liquid clustering earn its keep? One run, three arms, the dashboard's two
real query shapes.

```bash
python scripts/bench_serving.py --setup      # build arms B and C in the dev sandbox
python scripts/bench_serving.py --run        # the timing matrix
python scripts/bench_serving.py --teardown   # drop the bench tables
```

## Result

Materializing the view roughly **halves dashboard latency**; clustering the
materialized table adds nothing at this data size and is slightly worse.

| arm | shape | n | median ms | min ms | p10–p90 ms | exec ms | read_bytes | read_files | pruned_files | rows |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| A_view | overview | 7 | 767 | 734 | 737–845 | 361 | 5,222,815 | 6 | 0 | 62 |
| A_view | drilldown | 7 | 956 | 905 | 920–979 | 451 | 10,534,255 | 5 | 1 | 2,707 |
| B_table | overview | 7 | **341** | 319 | 323–389 | 89 | 1,809,393 | 1 | 0 | 62 |
| B_table | drilldown | 7 | **535** | 476 | 481–592 | 144 | 17,068,859 | 1 | 0 | 2,707 |
| C_clustered | overview | 7 | 447 | 380 | 381–532 | 137 | 1,776,474 | 1 | 0 | 62 |
| C_clustered | drilldown | 7 | 658 | 627 | 629–714 | 303 | 33,429,798 | 1 | 0 | 2,707 |

- **A → B, the materialization win.** The overview a dashboard user waits for
  drops from 767 ms to 341 ms (median of 7); the per-member drill-down from
  956 ms to 535 ms. Warehouse execution time falls further — 361 → 89 ms and
  451 → 144 ms — because the view re-runs a 4-way LEFT JOIN over 587,758 votes
  on every query and the table has that join paid down at build time. Bytes
  scanned for the overview drop 5.2 MB → 1.8 MB (6 files touched → 1).
- **B → C, the clustering null.** Clustered arm C is *slower* than plain arm B
  on both shapes (447 vs 341 ms, 658 vs 535 ms) and reads about twice the bytes
  on the drill-down. This is not a knock on liquid clustering — it is the
  expected result at this size, measured instead of assumed. See below.

## Why clustering can't win here

Liquid clustering works by letting queries **skip files**. The whole
materialized table is 587,758 rows, 84 MB — **one Delta file**. With one file
there is nothing to skip: every query reads it whether clustered or not, so
clustering can only shuffle row order, and here the shuffle hurt — reordering
by `(member_name, vote_date)` broke the natural insertion-order locality and
the drill-down read 33 MB against unclustered B's 17 MB.

The crossover math from the layout gate: at 142.2 bytes/row, a second 128 MB
file needs ~943,598 rows — about **42 years of SF votes** at the observed
~22.6K/yr. Clustering is the right tool roughly two orders of magnitude of
data later than this warehouse is.

## What the numbers mean for the pipeline

The recommendation is to **materialize `member_vote_record` as a table in the
dbt run and skip clustering**. Cost of the win: 84 MB of storage and one CTAS-
sized build step in the weekly job, against ~0.4 s shaved off every dashboard
interaction. The dashboard is the project's non-technical consumer; sub-second
vs. near-second is the difference a person feels.

## Protocol

- Arms: A = `gold.member_vote_record` as it ships (a view); B = the same rows
  CTAS'd to a table; C = the same CTAS `CLUSTER BY (member_name, vote_date)`
  + `OPTIMIZE`. B and C live in the `dev_jacksoncdawson` sandbox; production
  gold is only ever read.
- Shapes: the dashboard's two real queries — the overview (counts per member
  per vote value in the default two-year window, 2024-07-01..2026-06-30) and
  the drill-down (one member's votes in that window). Drill-down member is the
  **median** of the per-member vote-count distribution (Bilal Mahmood, 2,707
  rows in window) — the busiest member would make it a scan test, a rare one a
  round-trip test.
- 2 warmups discarded per (arm, shape), then 7 timed runs, interleaved
  round-robin with the arm order rotated each round so warehouse drift lands on
  all arms equally. Result cache disabled and independently verified off per
  statement (`from_result_cache` asserted false on all 42).
- Latency is client-side around execute + fetchall — what the dashboard user
  actually waits for. Bytes/files come from `system.query.history`, matched on
  a per-statement nonce. Row counts agreed across arms (587,758) and the Delta
  version of both bench tables was unchanged across the run.
- 0 of 42 executions rejected (no cache hits, no queueing, no missing metrics).
  One operational note: `system.query.history` lagged past the harness's
  5-minute poll during the run, so the scan metrics were re-pulled by nonce a
  few minutes later and merged with the captured client timings — same rows,
  same grading, just collected late.

## What this does and does not show

- **Medians of 7 on a warm serverless warehouse.** Cold-start latency is
  excluded by design (first `select 1` was 269 ms — warehouse was warm). A
  cold dashboard load pays warehouse spin-up on any arm.
- **Two query shapes, one window.** These are the dashboard's actual queries,
  not a synthetic suite; other shapes (the NL agent's ad-hoc SQL) were not
  measured.
- **The clustering result is size-bound, not general.** C's loss is the
  honest small-data answer; do not read it as "clustering doesn't work."
