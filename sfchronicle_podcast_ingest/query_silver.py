#!/usr/bin/env python3
"""
Example SQL queries against the local silver SQLite DB (free).

Usage:
  ./.venv/bin/python3 query_silver.py
  ./.venv/bin/python3 query_silver.py --bill prop_c
  ./.venv/bin/python3 query_silver.py --topic homelessness
  ./.venv/bin/python3 query_silver.py --person scott_wiener
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_SQLITE = os.path.join(
    os.path.dirname(__file__),
    "data",
    "podcast_silver.sqlite",
)
SQLITE_PATH = os.getenv("SILVER_SQLITE_PATH", DEFAULT_SQLITE)


def run_query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    if not rows:
        print("(no rows)")
        return
    print(" | ".join(cols))
    print("-" * 80)
    for row in rows[:25]:
        print(" | ".join("" if v is None else str(v) for v in row))
    if len(rows) > 25:
        print(f"... {len(rows) - 25} more rows")


def main() -> int:
    parser = argparse.ArgumentParser(description="Query local podcast silver SQLite")
    parser.add_argument("--db", default=SQLITE_PATH, help="Path to SQLite file")
    parser.add_argument("--bill", help="bill_normalized, e.g. prop_c or ab_1487")
    parser.add_argument("--topic", help="topic key, e.g. homelessness")
    parser.add_argument("--person", help="person_normalized, e.g. scott_wiener")
    parser.add_argument("--show", help="show_slug filter")
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(
            f"Missing silver DB at {args.db}\n"
            "Build it with: ./run_silver.sh   (or ./run_local_pipeline.sh)",
            file=sys.stderr,
        )
        return 1

    conn = sqlite3.connect(args.db)
    try:
        if args.bill:
            sql = """
                SELECT e.show_slug, e.title, e.pub_date, b.bill_ref, b.quote
                FROM episode_bills b
                JOIN episodes e USING (episode_id)
                WHERE b.bill_normalized = ?
                  AND e.usable = 1
            """
            params: list = [args.bill.lower()]
            if args.show:
                sql += " AND e.show_slug = ?"
                params.append(args.show)
            sql += " ORDER BY e.pub_date DESC"
            run_query(conn, sql, tuple(params))
        elif args.topic:
            sql = """
                SELECT e.show_slug, e.title, e.pub_date, t.topic, t.score, t.quote
                FROM episode_topics t
                JOIN episodes e USING (episode_id)
                WHERE t.topic = ?
                  AND e.usable = 1
            """
            params = [args.topic.lower()]
            if args.show:
                sql += " AND e.show_slug = ?"
                params.append(args.show)
            sql += " ORDER BY t.score DESC, e.pub_date DESC"
            run_query(conn, sql, tuple(params))
        elif args.person:
            sql = """
                SELECT e.show_slug, e.title, e.pub_date, p.person_name, p.role_hint, p.quote
                FROM episode_people p
                JOIN episodes e USING (episode_id)
                WHERE p.person_normalized = ?
                  AND e.usable = 1
            """
            params = [args.person.lower()]
            if args.show:
                sql += " AND e.show_slug = ?"
                params.append(args.show)
            sql += " ORDER BY e.pub_date DESC"
            run_query(conn, sql, tuple(params))
        else:
            print("=== Counts ===")
            run_query(
                conn,
                """
                SELECT
                  (SELECT COUNT(*) FROM episodes) AS episodes,
                  (SELECT COUNT(*) FROM episodes WHERE usable=1) AS usable,
                  (SELECT COUNT(*) FROM episode_bills) AS bill_rows,
                  (SELECT COUNT(*) FROM episode_topics) AS topic_rows,
                  (SELECT COUNT(*) FROM episode_people) AS people_rows
                """,
            )
            print("\n=== Top topics ===")
            run_query(
                conn,
                """
                SELECT topic, COUNT(*) AS episodes, SUM(score) AS total_score
                FROM episode_topics
                GROUP BY topic
                ORDER BY episodes DESC
                LIMIT 15
                """,
            )
            print("\n=== Top bill refs ===")
            run_query(
                conn,
                """
                SELECT bill_normalized, bill_ref, COUNT(*) AS episodes
                FROM episode_bills
                GROUP BY bill_normalized, bill_ref
                ORDER BY episodes DESC
                LIMIT 15
                """,
            )
            print("\nTip: query_silver.py --bill prop_c | --topic homelessness | --person scott_wiener")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
