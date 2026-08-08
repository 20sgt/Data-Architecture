#!/usr/bin/env python3
"""Demo UI: ask a question, see the answer, the SQL, the rows, the episodes.

Run:  streamlit run app/streamlit_app.py
"""
import streamlit as st

import ask

EXAMPLES = [
    "How did each supervisor vote on housing legislation this year?",
    "Which supervisor votes 'No' most often?",
    "How many matters about homelessness passed vs. were filed?",
    "What legislation is still in committee right now?",
]

st.set_page_config(page_title="SF Legislation Q&A", layout="wide")
st.title("San Francisco Legislation — ask a question")
st.caption(
    "Natural language → SQL over the gold star schema, plus a scan of "
    "1,832 SF news podcast episodes for related listening."
)


@st.cache_resource
def warehouse():
    """One warehouse connection for the whole session (cold start is slow)."""
    return ask.connect()


with st.sidebar:
    st.subheader("Try one")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state.q = ex

question = st.text_input(
    "Question", value=st.session_state.get("q", ""), placeholder=EXAMPLES[0]
)

if question:
    with st.spinner("Planning the query, running it, searching transcripts…"):
        try:
            out = ask.ask(question, conn=warehouse())
        except Exception as exc:  # surface the failure rather than a blank page
            st.error(f"{type(exc).__name__}: {exc}")
            st.stop()

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
