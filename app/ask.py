#!/usr/bin/env python3
"""Natural-language question -> SQL on the gold star schema + podcast episode.

Two Claude calls per question:
  1. plan     — question -> {sql, search_terms}
  2. answer   — SQL rows + transcript hits -> a written answer

The gold schema is read live from the warehouse (DESCRIBE), so the prompt can
never drift from the tables dbt actually built.

Run:  python app/ask.py "how did Dorsey vote on housing bills this year?"
"""
import json
import os
import re
import sqlite3
import sys
from datetime import date

import anthropic
from databricks import sql as dbsql
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
PODCAST_DB = os.path.join(HERE, "data", "podcast_search.sqlite")
SCHEMA_CACHE = os.path.join(HERE, "data", "gold_schema.txt")

CATALOG = os.getenv("DBT_DATABRICKS_CATALOG", "corn_off_the_cob")
MODEL = "claude-opus-5"
ROW_LIMIT = 200

client = anthropic.Anthropic()


# ---------------------------------------------------------------- warehouse

CREDENTIALS = ("DBT_DATABRICKS_HOST", "DBT_DATABRICKS_HTTP_PATH", "DBT_DATABRICKS_TOKEN")


def connect():
    # Nothing loads .env for us. A bare KeyError here reads like a bug in the
    # app; it is almost always a shell that never sourced the file.
    missing = [k for k in CREDENTIALS if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"No warehouse credentials: {', '.join(missing)} not set. "
            "Run `set -a; source .env; set +a` first (see .env.example)."
        )
    return dbsql.connect(
        server_hostname=os.environ["DBT_DATABRICKS_HOST"],
        http_path=os.environ["DBT_DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DBT_DATABRICKS_TOKEN"],
    )


def gold_schema(conn, refresh=False):
    """Column list for every gold table, as text for the prompt.

    Cached to disk: the schema changes only when dbt adds a model, and the
    DESCRIBEs are ~10 round trips we don't want on every question.
    """
    if os.path.exists(SCHEMA_CACHE) and not refresh:
        with open(SCHEMA_CACHE) as fh:
            return fh.read()

    out = []
    with conn.cursor() as cur:
        cur.execute(f"SHOW TABLES IN {CATALOG}.gold")
        tables = [r[1] for r in cur.fetchall()]
        for t in tables:
            cur.execute(f"DESCRIBE TABLE {CATALOG}.gold.{t}")
            cols = [f"{r[0]} {r[1]}" for r in cur.fetchall() if r[0] and not r[0].startswith("#")]
            out.append(f"{CATALOG}.gold.{t}(" + ", ".join(cols) + ")")
    text = "\n".join(out)
    with open(SCHEMA_CACHE, "w") as fh:
        fh.write(text)
    return text


# ------------------------------------------------------------------- guard

def guard_sql(sql, limit=ROW_LIMIT):
    """Read-only check + row cap. This is the trust boundary for model output.

    The model is asked for a single SELECT; anything else is refused rather
    than sanitized, because a rewrite that "fixes" a mutating statement is a
    much easier thing to get subtly wrong than a flat rejection.
    """
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty SQL")
    if ";" in s:
        raise ValueError("refusing multiple statements")
    if not re.match(r"^\s*(select|with)\b", s, re.I):
        raise ValueError(f"refusing non-SELECT statement: {s[:60]!r}")
    # Word-boundary match so a column named e.g. `updated_at` isn't a false hit.
    banned = r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy)\b"
    hit = re.search(banned, s, re.I)
    if hit:
        raise ValueError(f"refusing statement containing {hit.group(0).upper()}")
    if not re.search(r"\blimit\s+\d+\s*$", s, re.I):
        s += f"\nLIMIT {limit}"
    return s


def run_sql(conn, sql, params=None):
    """Run a SELECT. `params` binds :named markers — use it for anything the
    user picked, so a member name with an apostrophe is data, never syntax."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return cols, [list(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- podcasts

# The RSS channel titles. Nothing upstream stores them: ingest.py's SHOW_FEEDS
# is slug -> feed URL, and feedparser's channel title is parsed and thrown away,
# so the slug is all that reaches the index. Capturing the real title properly
# would mean a change to ingest.py, a silver rebuild and a full re-index — a lot
# of moving parts for a display string.
#
# ponytail: nine shows, hardcoded. Derive them from the slug instead and five of
# the nine come out wrong: "Fifth And Mission", "Giants Splash As Plus", and
# nothing in "fixing-our-city" tells you about the "SFNext:" prefix.
SHOW_NAMES = {
    "fifth-and-mission": "Fifth & Mission",
    "extra-spicy": "Extra Spicy",
    "giants-splash-as-plus": "Giants Splash",
    "datebook": "Datebook",
    "the-doodler": "The Doodler",
    "warriors-off-court": "Warriors Off Court",
    "fixing-our-city": "SFNext: Fixing Our City",
    "chronicled-kamala-harris": "Chronicled: Who Is Kamala Harris?",
    "voice-of-san-francisco": "Voice of San Francisco",
}


def show_name(slug):
    """Display title for a show. An unknown slug degrades to a tidied version of
    itself, so adding a feed upstream shows something readable without a code
    change here — just not the show's real punctuation."""
    return SHOW_NAMES.get(slug) or (slug or "").replace("-", " ").title()


def search_podcasts(terms, k=3):
    """Top-k episodes by BM25 over ~60s transcript chunks."""
    if not terms.strip() or not os.path.exists(PODCAST_DB):
        return []
    conn = sqlite3.connect(PODCAST_DB)
    try:
        # Ranked by chunk, then deduped to one row per episode below. SQLite
        # won't allow bm25() inside an aggregate, so "best chunk per episode"
        # can't be a GROUP BY — take the top chunks in order and keep the first
        # hit for each episode, which is that episode's best-scoring chunk.
        rows = conn.execute(
            """select e.episode_id, e.title, e.show_slug, e.pub_date, e.source_url,
                      c.start_s, snippet(chunks, 0, '**', '**', '...', 24) as snip
               from chunks c join episodes e using(episode_id)
               where chunks match ?
               order by bm25(chunks) limit ?""",
            (terms, k * 20),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 rejects malformed match syntax (stray quotes, bare operators).
        # A bad search shouldn't sink the SQL half of the answer.
        return []
    finally:
        conn.close()

    out, seen = [], set()
    for episode_id, title, show, date, url, start, snip in rows:
        if episode_id in seen:
            continue
        seen.add(episode_id)
        out.append({"title": title, "show": show_name(show), "date": date, "url": url,
                    "at": f"{int(start)//60}:{int(start) % 60:02d}", "quote": snip})
        if len(out) == k:
            break
    return out


# ------------------------------------------------------------------ claude

class Plan(BaseModel):
    sql: str = Field(description="One Databricks SQL SELECT answering the question.")
    search_terms: str = Field(
        description="SQLite FTS5 MATCH query for the podcast transcripts: quoted "
        "phrases and bare words joined with OR. No trailing operators."
    )


PLAN_SYSTEM = """You translate questions about San Francisco Board of Supervisors \
legislation into SQL, and into keywords for searching SF news-podcast transcripts.

Gold star schema (Databricks SQL, Unity Catalog):
{schema}

Rules for `sql`:
- One SELECT (a leading CTE is fine). Never write to anything.
- Fully qualify tables as {catalog}.gold.<table>.
- Prefer {catalog}.gold.member_vote_record — it denormalizes votes with member,
  legislation, outcome, and meeting, so most questions need no joins.
- Aggregate rather than dumping rows when the question asks "how many"/"which".
- When the question is about legislation, aggregate to ONE ROW PER MATTER, not
  one row per member. Carry `matter_file`, `matter_name`, `matter_type` and
  `final_disposition` through, and put the vote split in that same row — e.g.
  count(*), and count_if(vote_value = 'Aye') / count_if(vote_value in ('No','Nay')).
  A table of per-member totals says how often people voted; it cannot say what
  they voted ON, and that is usually what was asked.
- Skip `matter_title` unless the question needs the full legal text. It runs to
  several hundred characters per row. `matter_name` is the readable summary.
- Aim for under ~150 rows. One row per vote overflows on any broad topic —
  "housing this year" alone is 1,633 votes across 124 matters. If a per-matter
  query would still be huge, rank and take the top N by vote count or recency,
  and make the ordering obvious in the SQL.

Rules for `search_terms`:
- The topic in plain words, not the SQL. Bill numbers and names help.
- Example: "homeless OR homelessness OR encampment OR \\"navigation center\\"".
"""


def plan(question, schema):
    # No `effort` here: parse() builds output_config.format itself, and passing
    # our own output_config could clobber it. Default effort it is — if planning
    # latency hurts the demo, try effort "medium" and confirm parsing still works.
    r = client.messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=PLAN_SYSTEM.format(schema=schema, catalog=CATALOG),
        messages=[{"role": "user", "content": question}],
        output_format=Plan,
    )
    return r.parsed_output


ANSWER_SYSTEM = """You answer questions about San Francisco legislation for a \
general audience. You are given the question, the SQL that ran, its result rows, \
and excerpts from SF news podcasts.

Summarize what the data shows in a few sentences — cite the actual numbers. Then, \
if a podcast excerpt is genuinely on-topic, point the reader to it by show, \
episode title, and timestamp. Never invent numbers that aren't in the rows.

When the rows carry legislation — a matter file number, a name, a type, an \
outcome — name the actual bills. Lead with the shape of the record, then make it \
concrete: what the notable matters were, how the board split on them, and what \
became of them. Cite a matter as its name with the file number in parentheses, \
e.g. Grant Agreement - Permanent Supportive Housing (251263). A reader who \
asked how their supervisor voted wants to know which bills those were, not only \
how many times each person said Aye.

`row_count` is the true number of rows the query returned; `rows` may be only \
the first slice of them. Take totals from `row_count`, and treat any matter you \
name as an example rather than implying you saw the whole list.

The transcript search always returns its best matches, so most of what you are \
handed will be off-topic. Never stretch to make one fit. When nothing is \
genuinely relevant, just end the answer — say nothing about podcasts at all. Do \
not write that no episode was relevant, that the search found nothing, or any \
other note about their absence: the reader did not ask about podcasts, and a \
sentence explaining that there is nothing to say is worse than silence.

Dates are a number too. A query written with CURRENT_DATE() returns rows with no \
year in them, so the period covered is NOT visible in the results — read it off \
`today` and the SQL, or describe the period in the question's own words ("this \
year") rather than naming a year the rows don't state."""


def answer(question, sql, cols, rows, episodes):
    payload = {
        "question": question,
        # The rows a CURRENT_DATE() query returns carry no year, and the model
        # will confidently supply the wrong one if we don't say what today is.
        "today": date.today().isoformat(),
        "sql": sql,
        "columns": cols,
        "rows": rows[:60],
        "row_count": len(rows),
        "podcast_hits": episodes,
    }
    r = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        output_config={"effort": "low"},
        system=ANSWER_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
    )
    return "".join(b.text for b in r.content if b.type == "text")


def ask(question, conn=None):
    """Full loop. Returns everything the UI needs to show its work."""
    own = conn is None
    conn = conn or connect()
    try:
        p = plan(question, gold_schema(conn))
        sql = guard_sql(p.sql)
        cols, rows = run_sql(conn, sql)
        episodes = search_podcasts(p.search_terms)
        return {
            "sql": sql,
            "columns": cols,
            "rows": rows,
            "episodes": episodes,
            "answer": answer(question, sql, cols, rows, episodes),
        }
    finally:
        if own:
            conn.close()


def demo():
    # connect() must refuse with a legible message, not a bare KeyError, when
    # the shell never sourced .env — the most common way to start this app.
    saved = {k: os.environ.pop(k) for k in CREDENTIALS if k in os.environ}
    try:
        connect()
        raise AssertionError("connect() ran with no credentials")
    except RuntimeError as exc:
        assert "source .env" in str(exc), exc
    finally:
        os.environ.update(saved)

    assert guard_sql("select 1").endswith("LIMIT 200")
    assert guard_sql("SELECT 1 limit 5") == "SELECT 1 limit 5"          # cap respected
    assert guard_sql("with a as (select 1) select * from a").startswith("with")
    for bad in ["drop table gold.fact_vote",
                "select 1; drop table t",
                "delete from gold.dim_matter",
                "update x set y=1"]:
        try:
            guard_sql(bad)
            raise AssertionError(f"guard let through: {bad}")
        except ValueError:
            pass
    assert "LIMIT" in guard_sql("select * from t where updated_at > '2026-01-01'")

    assert show_name("fifth-and-mission") == "Fifth & Mission"
    assert show_name("nope-not-a-show") == "Nope Not A Show"   # unknown -> readable
    assert show_name("") == "" and show_name(None) == ""       # never crash the row

    # Search must actually return hits — a broken query otherwise fails silently
    # through the OperationalError guard and just looks like "no relevant episode".
    if os.path.exists(PODCAST_DB):
        hits = search_podcasts("homeless OR homelessness OR encampment")
        assert hits, "expected transcript hits for a common SF topic"
        assert all(h["show"] not in SHOW_NAMES for h in hits), "slug leaked to the UI"
        assert len({h["title"] for h in hits}) == len(hits), "duplicate episodes"
        assert search_podcasts('bad ) syntax "') == []       # malformed match
        assert search_podcasts("zzzznonexistentterm") == []  # no hits
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif len(sys.argv) > 1:
        out = ask(" ".join(sys.argv[1:]))
        print(out["sql"], "\n")
        print(out["answer"])
    else:
        print(__doc__)
