#!/usr/bin/env python3
"""Demo UI. Two ways at the same gold tables:

  Ask            — type a question, see the answer, the SQL, the rows, the episodes.
  Voting record  — the fixed charts: how each supervisor voted, and on what.

Run:  streamlit run app/streamlit_app.py
"""
import datetime as dt

import streamlit as st

import ask
import dashboard

EXAMPLES = [
    "How did each supervisor vote on housing legislation this year?",
    "Which supervisor votes 'No' most often?",
    "How many matters about homelessness passed vs. were filed?",
    "What legislation is still in committee right now?",
]

st.set_page_config(page_title="SF Legislation", layout="wide")
st.title("San Francisco Board of Supervisors")


@st.cache_resource
def warehouse():
    """One warehouse connection for the whole session (cold start is slow)."""
    return ask.connect()


# Both tabs render on every rerun, so an uncached dashboard query would hit the
# warehouse each time someone typed a character into the Ask box. `_conn` is
# underscore-prefixed so Streamlit skips hashing the connection object.
@st.cache_data(ttl=3600, show_spinner=False)
def _bounds(_conn):
    return dashboard.vote_date_bounds(_conn)


@st.cache_data(ttl=3600, show_spinner="Counting votes…")
def _overview(_conn, start, end):
    return dashboard.overview(_conn, start, end)


@st.cache_data(ttl=3600, show_spinner="Pulling the vote list…")
def _member_votes(_conn, member, start, end):
    return dashboard.member_votes(_conn, member, start, end)


def render_ask():
    st.caption(
        "Natural language → SQL over the gold star schema, plus a scan of "
        "1,832 SF news podcast episodes for related listening."
    )
    question = st.text_input(
        "Question", value=st.session_state.get("q", ""), placeholder=EXAMPLES[0]
    )
    if not question:
        return

    with st.spinner("Planning the query, running it, searching transcripts…"):
        try:
            out = ask.ask(question, conn=warehouse())
        except Exception as exc:  # surface the failure rather than a blank page
            st.error(f"{type(exc).__name__}: {exc}")
            return  # not st.stop() — that would blank the other tab too

    st.markdown(out["answer"])

    if out["episodes"]:
        st.subheader("Related listening")
        for e in out["episodes"]:
            st.markdown(
                f"**{e['title']}** — {e['show']} · {e['date']} · at **{e['at']}**"
                + (f"  \n[Listen]({e['url']})" if e["url"] else "")
            )
            st.caption(f"…{e['quote']}…")

    with st.expander(f"Query and results ({len(out['rows'])} rows)"):
        st.code(out["sql"], language="sql")
        # rows come back as lists; zip them back to their column names
        st.dataframe(
            {c: [r[i] for r in out["rows"]] for i, c in enumerate(out["columns"])},
            hide_index=True,
        )


def render_dashboard():
    conn = warehouse()
    first, last = _bounds(conn)

    # Gold carries 26 years. Charting all of it at once is unreadable, so the
    # default window is the trailing two years and the control does the rest.
    c1, c2 = st.columns([3, 1])
    period = c1.date_input(
        "Period",
        value=(max(first, last - dt.timedelta(days=730)), last),
        min_value=first,
        max_value=last,
    )
    top_n = c2.number_input("Members shown", 1, 50, 11, help="The Board seats 11.")

    # Mid-selection the widget returns just the start date. Wait for the second.
    if len(period) != 2:
        st.info("Pick an end date.")
        return
    start, end = period

    rows = _overview(conn, start, end)
    if not rows:
        st.info(f"No votes recorded between {start} and {end}.")
        return

    tally = sorted(dashboard.totals(rows).items(), key=lambda kv: -kv[1])
    strip = [("Total", sum(n for _, n in tally))] + tally
    for col, (label, n) in zip(st.columns(len(strip)), strip):
        col.metric(label, f"{n:,}")

    st.subheader("How each supervisor voted")
    wide = dashboard.pivot(rows, top_n=top_n)
    st.bar_chart(
        wide,
        x="member",
        y=[c for c in wide if c != "member"],
        stack=False,          # grouped bars, one per vote value — not a stack
        y_label="votes",      # x stays alphabetical: sort=False blanks a grouped chart
        x_label="",
        height=420,
    )

    st.subheader("Every vote, one supervisor")
    member = st.selectbox("Supervisor", wide["member"])
    cols, votes = _member_votes(conn, member, start, end)
    st.caption(f"{len(votes):,} votes · {start} to {end}")
    st.dataframe(
        {c: [r[i] for r in votes] for i, c in enumerate(cols)},
        hide_index=True,
        use_container_width=True,
    )


with st.sidebar:
    st.subheader("Example questions")
    st.caption("For the Ask tab.")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state.q = ex

tab_ask, tab_dash = st.tabs(["Ask", "Voting record"])
with tab_ask:
    render_ask()
with tab_dash:
    render_dashboard()
