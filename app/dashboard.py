#!/usr/bin/env python3
"""Charted voting record, straight off gold.member_vote_record.

The other half of the serving layer: `ask.py` answers whatever you type,
this answers the one question the project was built for — how did each
supervisor vote, and on what.

Query layer only, no Streamlit. `streamlit_app.py` owns the widgets and the
caching; keeping the SQL here is what lets the pivot logic self-check offline:

    python app/dashboard.py --demo
"""
import sys

import ask

# Legistar's own labels, most→least common. `vote_value` is a raw passthrough
# with no accepted_values test on it (see scrape/history_detail.py), so this is
# a display order, NOT a whitelist — pivot() appends anything it hasn't seen.
VOTE_ORDER = ["Aye", "No", "Nay", "Excused", "Absent", "Recused", "Present"]

# ponytail: no district here. dim_person is identity-only — person_id + name.
# The old frontend hardcoded an 11-person person_id->district map, which is
# wrong for every member the 26-year backfill introduced. Populating it needs
# the Legistar web API people slice (see TODO.md), not a constant.
RELATION = "gold.member_vote_record"


def _rel():
    return f"{ask.CATALOG}.{RELATION}"


# ------------------------------------------------------------------ queries

def vote_date_bounds(conn):
    """(first, last) vote date on record — the date picker's limits."""
    _, rows = ask.run_sql(conn, f"select min(vote_date), max(vote_date) from {_rel()}")
    return rows[0][0], rows[0][1]


# Long enough to survive the Board's summer recess, short enough to drop a
# member at the end of their term. Members leaving in Jan 2025 last voted
# 2024-12-17; the sitting eleven all voted within days of each other.
ACTIVE_DAYS = 120


def active_members(conn, days=ACTIVE_DAYS):
    """Members still serving: those who voted recently.

    There is no term data in gold — dim_person is identity-only, with no
    district, party, or start/end dates. "Still serving" therefore has to be
    inferred, and the only honest signal is whether they are still casting
    votes.

    Recency is measured against the newest vote in the table, NOT today. The
    Board recesses each summer, so "voted in the last 120 days" measured from
    the wall clock would empty out every August.
    """
    _, rows = ask.run_sql(
        conn,
        f"""select member_name
            from {_rel()}
            where member_name is not null
            group by member_name
            having max(vote_date) >= (select max(vote_date) from {_rel()})
                                     - interval {int(days)} days""",
    )
    return {r[0] for r in rows}


def overview(conn, start, end):
    """(member_name, vote_value, n) for every member with a vote in the window.

    Not capped in SQL: the window bounds the member set, and applying "top N"
    in pivot() means the cached result serves every N without a re-query.
    """
    _, rows = ask.run_sql(
        conn,
        f"""select member_name, vote_value, count(*) as n
            from {_rel()}
            where vote_date between :start and :end
              and member_name is not null
            group by member_name, vote_value""",
        {"start": str(start), "end": str(end)},
    )
    return rows


def member_votes(conn, member, start, end):
    """(columns, rows) — one member's votes in the window, newest first."""
    return ask.run_sql(
        conn,
        f"""select vote_date, vote_value, matter_file, matter_name, matter_type,
                   committee_name, lifecycle, final_disposition, meeting_date
            from {_rel()}
            where member_name = :member
              and vote_date between :start and :end
            order by vote_date desc""",
        {"member": member, "start": str(start), "end": str(end)},
    )


# -------------------------------------------------------------------- shape

def pivot(rows, only=None):
    """Long (member, vote_value, n) -> wide dict for st.bar_chart.

    Returns {"member": [...], "<vote value>": [...], ...}. Members are sorted
    by total votes desc; vote columns follow VOTE_ORDER with unrecognized
    labels appended alphabetically, so a new Legistar label shows up as its
    own bar instead of vanishing.

    `only` restricts to a set of member names (see active_members). Every
    member in the period is charted otherwise — no top-N truncation, which
    drops people silently and answers a question nobody asked.
    """
    totals, counts = {}, {}
    for member, value, n in rows:
        if only is not None and member not in only:
            continue
        value = value or "(blank)"
        totals[member] = totals.get(member, 0) + n
        counts[(member, value)] = counts.get((member, value), 0) + n

    members = sorted(totals, key=lambda m: (-totals[m], m))

    seen = {v for _, v in counts}
    values = [v for v in VOTE_ORDER if v in seen] + sorted(seen - set(VOTE_ORDER))

    out = {"member": members}
    for v in values:
        out[v] = [counts.get((m, v), 0) for m in members]
    return out


def totals(rows, only=None):
    """{vote_value: n} — the tally strip. Same `only` filter as pivot(), so
    the headline numbers always describe the members actually charted."""
    out = {}
    for member, value, n in rows:
        if only is not None and member not in only:
            continue
        value = value or "(blank)"
        out[value] = out.get(value, 0) + n
    return out


# --------------------------------------------------------------------- demo

def demo():
    rows = [
        ("Chan", "Aye", 10), ("Chan", "No", 2),
        ("Dorsey", "Aye", 30), ("Dorsey", "Excused", 1), ("Dorsey", "Zzz", 4),
    ]
    p = pivot(rows)
    # Dorsey (35) outranks Chan (12); VOTE_ORDER first, unknown label last.
    assert p["member"] == ["Dorsey", "Chan"], p["member"]
    assert list(p) == ["member", "Aye", "No", "Excused", "Zzz"], list(p)
    assert p["Aye"] == [30, 10]
    assert p["No"] == [0, 2], "member missing a vote value must zero-fill, not misalign"
    assert p["Zzz"] == [4, 0], "unknown Legistar label must survive"

    assert pivot(rows, only={"Chan"})["member"] == ["Chan"]
    assert pivot(rows, only={"Chan"})["Aye"] == [10], "filter must not skew counts"
    assert "Zzz" not in pivot(rows, only={"Chan"}), "dropped member's labels go too"
    assert pivot(rows, only=set())["member"] == []   # empty set filters all, not none
    assert pivot([]) == {"member": []}

    # A NULL vote_value is a real possibility on a LEFT-joined view.
    assert pivot([("Chan", None, 3)])["(blank)"] == [3]

    assert totals(rows) == {"Aye": 40, "No": 2, "Excused": 1, "Zzz": 4}
    # The strip must agree with the chart, or the headline number describes a
    # different set of people than the bars underneath it.
    assert totals(rows, only={"Chan"}) == {"Aye": 10, "No": 2}
    assert sum(totals(rows, only={"Chan"}).values()) == sum(
        pivot(rows, only={"Chan"})[c][0] for c in pivot(rows, only={"Chan"}) if c != "member")
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        print(__doc__)
