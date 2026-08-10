#!/usr/bin/env python3
"""Benchmark the serving path: view vs. table vs. liquid-clustered table.

`gold.member_vote_record` is a VIEW, so every dashboard query re-runs a 4-way
LEFT JOIN over fact_vote. Three arms, one set of queries:

    A_view       gold.member_vote_record, as it ships today
    B_table      the same rows, materialized, unclustered
    C_clustered  the same rows, CLUSTER BY (member_name, vote_date) + OPTIMIZE

A->B is the materialization win. B->C is the clustering win. Arms B and C are
built in a dev sandbox; production gold is only ever read.

    python scripts/bench_serving.py --setup      # build B and C, print the layout gate
    python scripts/bench_serving.py --run        # the timing matrix
    python scripts/bench_serving.py --teardown   # drop the bench tables
    python scripts/bench_serving.py --demo       # offline self-check

Latency is measured client-side, around execute + fetchall: that is what the
dashboard user waits for, and it needs no management-API token. Bytes and file
counts come from system.query.history afterwards, matched on a nonce embedded
in each statement, so there is no per-query polling in the timing loop.
"""
import argparse
import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone

CATALOG = os.getenv("DBT_DATABRICKS_CATALOG", "corn_off_the_cob")
BENCH_SCHEMA = os.getenv("BENCH_SCHEMA", "dev_jacksoncdawson")

VIEW = f"{CATALOG}.gold.member_vote_record"
TABLE = f"{CATALOG}.{BENCH_SCHEMA}.bench_mvr_table"
CLUSTERED = f"{CATALOG}.{BENCH_SCHEMA}.bench_mvr_lc"

ARMS = [("A_view", VIEW), ("B_table", TABLE), ("C_clustered", CLUSTERED)]
BENCH_TABLES = [TABLE, CLUSTERED]

# Pinned so the run is reproducible. Two years wide, matching the dashboard's
# default period, and ending inside the data (last vote on record: 2026-07-27).
START, END = "2024-07-01", "2026-06-30"

WARMUPS = 2          # discarded: absorbs Photon codegen for a new plan shape
RUNS = 7             # timed, interleaved round-robin across arms
HISTORY_WAIT_S = 300  # system.query.history is not instant


# ------------------------------------------------------------------ queries

def q_overview(rel):
    """The dashboard's overview: counts per member per vote value in a window."""
    return f"""select member_name, vote_value, count(*) as n
               from {rel}
               where vote_date between date'{START}' and date'{END}'
                 and member_name is not null
               group by member_name, vote_value
               order by n desc"""


def q_drilldown(rel, member):
    """The dashboard's drill-down: one member's votes in the window."""
    return f"""select vote_date, vote_value, matter_file, matter_name, matter_type,
                      committee_name, lifecycle, final_disposition, meeting_date
               from {rel}
               where member_name = '{member}'
                 and vote_date between date'{START}' and date'{END}'
               order by vote_date desc"""


SHAPES = {"overview": q_overview, "drilldown": q_drilldown}


# ------------------------------------------------------------------ helpers

def connect():
    from databricks import sql as dbsql
    return dbsql.connect(
        server_hostname=os.environ["DBT_DATABRICKS_HOST"],
        http_path=os.environ["DBT_DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DBT_DATABRICKS_TOKEN"],
    )


def sql(conn, statement, params=None):
    with conn.cursor() as cur:
        cur.execute(statement, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, [list(r) for r in cur.fetchall()]


def one(conn, statement):
    return sql(conn, statement)[1][0][0]


def tag(arm, shape, run, nonce):
    return f"{arm}:{shape}:{run}:{nonce}"


def stamp(statement, t):
    """Leading comment: defeats the result cache (which keys on statement text)
    and survives statement_text truncation in query history."""
    return f"/* bench:{t} */ " + statement


def parse_tag(statement_text):
    """Pull the tag back out of a history row. None if it isn't ours."""
    if not statement_text or not statement_text.startswith("/* bench:"):
        return None
    return statement_text[len("/* bench:"):].split(" */", 1)[0]


def pct(values, p):
    """Linear-interpolated percentile. p in [0, 1]."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rejected(hist):
    """Reasons this execution must not count toward the timings.

    A cached result measures nothing. Time spent waiting for compute or at
    capacity is warehouse state, not plan quality — it would land on whichever
    arm happened to run when the warehouse autoscaled.
    """
    why = []
    if hist.get("from_result_cache"):
        why.append("result_cache")
    if (hist.get("waiting_for_compute_duration_ms") or 0) > 0:
        why.append("waiting_for_compute")
    if (hist.get("waiting_at_capacity_duration_ms") or 0) > 0:
        why.append("waiting_at_capacity")
    return why


def describe_detail(conn, rel):
    cols, rows = sql(conn, f"describe detail {rel}")
    d = dict(zip(cols, rows[0]))
    # Array columns arrive as numpy arrays, which have no usable truth value.
    for k in ("clusteringColumns", "partitionColumns"):
        d[k] = list(d.get(k) if d.get(k) is not None else [])
    return d


def delta_version(conn, rel):
    return one(conn, f"select max(version) from (describe history {rel})")


# ------------------------------------------------------------------- setup

def setup(conn):
    print(f"# Building arms B and C in {CATALOG}.{BENCH_SCHEMA}\n")

    # Back to back from the same view, so B and C are identical in content.
    print(f"CTAS -> {TABLE}")
    sql(conn, f"create or replace table {TABLE} as select * from {VIEW}")

    print(f"CTAS -> {CLUSTERED}  (cluster by member_name, vote_date)")
    sql(conn, f"""create or replace table {CLUSTERED}
                  cluster by (member_name, vote_date)
                  as select * from {VIEW}""")

    # CLUSTER BY on a CTAS is best-effort on write; OPTIMIZE is what lays the
    # data out. Without it, arm C is arm B wearing a label.
    print(f"OPTIMIZE {CLUSTERED}")
    sql(conn, f"optimize {CLUSTERED}")

    print("\n## Layout gate\n")
    print("| table | numFiles | sizeInBytes | clusteringColumns | partitionColumns |")
    print("|---|---|---|---|---|")
    for rel in BENCH_TABLES:
        d = describe_detail(conn, rel)
        print(f"| {rel.split('.')[-1]} | {d['numFiles']} | {d['sizeInBytes']:,} | "
              f"{d.get('clusteringColumns')} | {d.get('partitionColumns')} |")

    b = describe_detail(conn, TABLE)
    c = describe_detail(conn, CLUSTERED)
    print()
    if not c.get("clusteringColumns"):
        print("!! CLUSTER BY did not take on arm C. B and C are the same table; "
              "the comparison would be pure noise. Stop and fix this first.")
    if b["numFiles"] == 1:
        print("!! Arm B is a SINGLE FILE. Liquid clustering skips files; with one "
              "file there is nothing to skip, so arm C cannot beat arm B on data\n"
              "   skipping no matter how well it clusters. Expect a null result, "
              "and report the crossover instead of a manufactured win.")
        rows = int(one(conn, f"select count(*) from {TABLE}"))
        per_row = b["sizeInBytes"] / rows
        print(f"\n   {rows:,} rows, {b['sizeInBytes']:,} bytes, {per_row:.1f} bytes/row.")
        for target_mb in (128, 1024):
            n = int(target_mb * 1024 * 1024 / per_row)
            print(f"   A second {target_mb} MB file needs ~{n:,} rows "
                  f"(~{n / 22600:,.0f} years of SF votes at ~22.6K/yr).")


def teardown(conn):
    for rel in BENCH_TABLES:
        print(f"DROP {rel}")
        sql(conn, f"drop table if exists {rel}")


# --------------------------------------------------------------------- run

def collect_history(conn, tags, since_ms):
    """Pull the metrics rows for our nonces. Returns {tag: row-dict}."""
    want = set(tags)
    found = {}
    deadline = time.time() + HISTORY_WAIT_S
    cols_wanted = """statement_text, total_duration_ms, execution_duration_ms,
                     compilation_duration_ms, result_fetch_duration_ms,
                     waiting_for_compute_duration_ms, waiting_at_capacity_duration_ms,
                     read_bytes, read_files, pruned_files, read_files_bytes,
                     pruned_files_bytes, read_rows, produced_rows,
                     read_io_cache_percent, from_result_cache, statement_id"""
    while time.time() < deadline:
        cols, rows = sql(conn, f"""
            select {cols_wanted}
            from system.query.history
            where start_time >= timestamp_millis({since_ms})
              and statement_text like '/* bench:%'""")
        for row in rows:
            d = dict(zip([c.strip() for c in cols], row))
            t = parse_tag(d["statement_text"])
            if t in want:
                found[t] = d
        missing = want - set(found)
        if not missing:
            return found
        print(f"  … waiting on {len(missing)} history rows", file=sys.stderr)
        time.sleep(20)
    return found


def run(conn):
    started_ms = int(time.time() * 1000)
    nonce = uuid.uuid4().hex[:10]

    # Belt: the session flag. Braces: a unique nonce per statement. Never trust
    # the flag alone — from_result_cache is asserted per run below.
    sql(conn, "set use_cached_result = false")

    t0 = time.perf_counter()
    sql(conn, "select 1")
    cold_s = time.perf_counter() - t0
    sql(conn, "select 1")

    counts = {arm: int(one(conn, f"select count(*) from {rel}")) for arm, rel in ARMS}
    versions_before = {r: delta_version(conn, r) for r in BENCH_TABLES}

    # The busiest member makes the drill-down a scan; a rare one makes it pure
    # round-trip overhead. Take the median of the distribution.
    _, dist = sql(conn, f"""select member_name, count(*) as n from {VIEW}
                            where vote_date between date'{START}' and date'{END}'
                              and member_name is not null
                            group by member_name order by n""")
    member, member_rows = dist[len(dist) // 2]

    print("## Environment\n")
    print(f"- run at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"- catalog: `{CATALOG}`, bench schema: `{BENCH_SCHEMA}`")
    print(f"- window: {START} .. {END}")
    print(f"- first `select 1`: {cold_s * 1000:,.0f} ms"
          + ("  **(cold warehouse — timings below are suspect)**" if cold_s > 20 else ""))
    print(f"- row counts: " + ", ".join(f"{a}={n:,}" for a, n in counts.items()))
    print(f"- drill-down member: **{member}** ({member_rows:,} rows in window)")
    print(f"- protocol: {WARMUPS} warmups discarded, {RUNS} timed runs per "
          f"(arm, shape), interleaved round-robin\n")

    if len(set(counts.values())) != 1:
        print("!! Arms disagree on row count — they are not the same data. Aborting.\n")
        return
    for rel in BENCH_TABLES:
        d = describe_detail(conn, rel)
        print(f"- `{rel.split('.')[-1]}`: {d['numFiles']} files, "
              f"{d['sizeInBytes']:,} bytes, clustering={d.get('clusteringColumns')}")
    print()

    # Warmups: per (arm, shape), discarded.
    for arm, rel in ARMS:
        for shape, build in SHAPES.items():
            stmt = build(rel) if shape == "overview" else build(rel, member)
            for w in range(WARMUPS):
                sql(conn, stamp(stmt, tag(arm, shape, f"warm{w}", nonce)))

    # Timed, interleaved: rotate the arm order each round so any drift in
    # warehouse state hits all three arms equally instead of the last one.
    results = []
    for i in range(RUNS):
        order = ARMS[i % len(ARMS):] + ARMS[:i % len(ARMS)]
        for arm, rel in order:
            for shape, build in SHAPES.items():
                stmt = build(rel) if shape == "overview" else build(rel, member)
                t = tag(arm, shape, str(i), nonce)
                start = time.perf_counter()
                _, rows = sql(conn, stamp(stmt, t))
                results.append({"tag": t, "arm": arm, "shape": shape, "run": i,
                                "client_ms": (time.perf_counter() - start) * 1000,
                                "rows": len(rows)})

    versions_after = {r: delta_version(conn, r) for r in BENCH_TABLES}
    moved = [r for r in BENCH_TABLES if versions_before[r] != versions_after[r]]
    if moved:
        print(f"!! Delta version moved mid-run on {moved} — something OPTIMIZEd or "
              "wrote to a bench table. Treat these numbers as void.\n")

    print("Pulling metrics from system.query.history…\n", file=sys.stderr)
    hist = collect_history(conn, [r["tag"] for r in results], started_ms)
    for r in results:
        h = hist.get(r["tag"], {})
        r["history"] = h
        r["rejects"] = rejected(h) if h else ["no_history_row"]

    report(results)


def report(results):
    print("## Results\n")
    print("| arm | shape | n | median ms | min ms | p10–p90 ms | exec ms | "
          "read_bytes | read_files | pruned_files | cache % | rows | rejected |")
    print("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")

    def med(rows, key):
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        return f"{statistics.median(vals):,.0f}" if vals else "—"

    for arm, _ in ARMS:
        for shape in SHAPES:
            group = [r for r in results if r["arm"] == arm and r["shape"] == shape]
            good = [r for r in group if not r["rejects"]]
            if not good:
                print(f"| {arm} | {shape} | 0 |" + " — |" * 10 + f" {len(group)} |")
                continue
            ms = [r["client_ms"] for r in good]
            h = [r["history"] for r in good]
            # Bytes scanned should be identical on every run of an arm. If it
            # isn't, the layout or the cache moved and the row is suspect.
            varied = len({x.get("read_bytes") for x in h}) > 1
            print(
                f"| {arm} | {shape} | {len(good)}"
                f" | {statistics.median(ms):,.0f} | {min(ms):,.0f}"
                f" | {pct(ms, .1):,.0f}–{pct(ms, .9):,.0f}"
                f" | {med(h, 'execution_duration_ms')}"
                f" | {med(h, 'read_bytes')}"
                f" | {med(h, 'read_files')}"
                f" | {med(h, 'pruned_files')}"
                f" | {med(h, 'read_io_cache_percent')}"
                f" | {good[0]['rows']:,} | {len(group) - len(good)} |"
                + ("  **read_bytes varied across runs**" if varied else ""))

    all_rejects = [r for r in results if r["rejects"]]
    print(f"\n{len(all_rejects)} of {len(results)} executions rejected.")
    for r in all_rejects:
        print(f"- `{r['tag']}`: {', '.join(r['rejects'])}")

    print("\n## Raw\n\n```json")
    for r in results:
        h = r["history"]
        print(json.dumps({k: v for k, v in r.items() if k != "history"}
                         | {"statement_id": h.get("statement_id"),
                            "read_bytes": h.get("read_bytes"),
                            "read_files": h.get("read_files"),
                            "pruned_files": h.get("pruned_files"),
                            "execution_duration_ms": h.get("execution_duration_ms"),
                            "compilation_duration_ms": h.get("compilation_duration_ms"),
                            "from_result_cache": h.get("from_result_cache")},
                         default=str))
    print("```")


# -------------------------------------------------------------------- demo

def demo():
    assert pct([1, 2, 3, 4, 5], 0.5) == 3
    assert pct([1, 2, 3, 4, 5], 0.0) == 1
    assert pct([1, 2, 3, 4, 5], 1.0) == 5
    assert pct([10], 0.9) == 10                      # single sample, no IndexError
    assert pct([1, 2], 0.5) == 1.5                   # interpolates
    assert statistics.median([9, 1, 5]) == 5         # order-independent

    t = tag("B_table", "overview", "3", "abc123")
    assert parse_tag(stamp("select 1", t)) == t
    assert parse_tag("select 1") is None             # not one of ours
    assert parse_tag("") is None
    assert parse_tag(None) is None

    nonces = {uuid.uuid4().hex[:10] for _ in range(2000)}
    assert len(nonces) == 2000, "nonce collision would silently merge two runs"

    assert rejected({"from_result_cache": True}) == ["result_cache"]
    assert rejected({"waiting_for_compute_duration_ms": 12}) == ["waiting_for_compute"]
    assert rejected({"waiting_at_capacity_duration_ms": 3}) == ["waiting_at_capacity"]
    # None is what query history returns for "did not wait" — must not reject.
    assert rejected({"from_result_cache": False,
                     "waiting_for_compute_duration_ms": None,
                     "waiting_at_capacity_duration_ms": 0}) == []
    print("ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for flag in ("setup", "run", "teardown", "demo"):
        ap.add_argument(f"--{flag}", action="store_true")
    args = ap.parse_args()

    if args.demo:
        return demo()
    if not (args.setup or args.run or args.teardown):
        return ap.print_help()

    conn = connect()
    try:
        if args.setup:
            setup(conn)
        if args.run:
            run(conn)
        if args.teardown:
            teardown(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
