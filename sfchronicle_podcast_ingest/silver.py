#!/usr/bin/env python3
"""
Build a free, queryable silver layer from enrichment JSON.

Writes:
  1) Local SQLite  — data/podcast_silver.sqlite  (SQL queries, $0)
  2) GCS JSONL     — podcasts/silver/*.jsonl     (same tables in the bucket)

No BigQuery / paid analytics APIs. Safe for local + Cloud Run weekly jobs.

Usage:
  ./.venv/bin/python3 silver.py
  ./.venv/bin/python3 silver.py --local-only
  ./.venv/bin/python3 silver.py --gcs-only
  ./.venv/bin/python3 silver.py --show fixing-our-city
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from ingest import get_storage_client, load_config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

ENRICHMENT_PREFIX = os.getenv("ENRICHMENT_PREFIX", "podcasts/enrichment")
SILVER_PREFIX = os.getenv("SILVER_PREFIX", "podcasts/silver")
DEFAULT_SQLITE = os.path.join(
    os.path.dirname(__file__),
    "data",
    "podcast_silver.sqlite",
)
SQLITE_PATH = os.getenv("SILVER_SQLITE_PATH", DEFAULT_SQLITE)

TABLE_FILES = (
    "episodes",
    "episode_bills",
    "episode_topics",
    "episode_people",
    "episode_stances",
    "episode_claims",
)


def normalize_person_key(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")


def first_quote(mentions: list[dict[str, Any]] | None) -> str | None:
    if not mentions:
        return None
    quote = mentions[0].get("quote")
    return quote if isinstance(quote, str) else None


def flatten_enrichment(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Turn one enrichment JSON into flat row lists for silver tables."""
    episode_id = record.get("episode_id") or ""
    show_slug = record.get("show_slug") or ""
    quality = record.get("quality") or {}
    summary = record.get("summary_fields") or {}
    usable = bool(quality.get("usable"))

    episodes = [
        {
            "episode_id": episode_id,
            "show_slug": show_slug,
            "title": record.get("title"),
            "pub_date": record.get("pub_date"),
            "usable": 1 if usable else 0,
            "char_count": quality.get("char_count"),
            "quality_reason": quality.get("reason"),
            "audio_gcs_uri": record.get("audio_gcs_uri"),
            "transcript_gcs_uri": record.get("transcript_gcs_uri"),
            "source_url": record.get("source_url"),
            "top_topics": json.dumps(summary.get("top_topics") or []),
            "bill_refs": json.dumps(summary.get("bill_refs") or []),
            "people_mentioned": json.dumps(summary.get("people_mentioned") or []),
            "enriched_at": record.get("enriched_at"),
            "engine": record.get("engine"),
        }
    ]

    bills: list[dict[str, Any]] = []
    for bill in record.get("bills") or []:
        bills.append(
            {
                "episode_id": episode_id,
                "show_slug": show_slug,
                "bill_ref": bill.get("ref"),
                "bill_normalized": bill.get("normalized"),
                "kind": bill.get("kind"),
                "quote": first_quote(bill.get("mentions")),
            }
        )

    topics: list[dict[str, Any]] = []
    for topic in record.get("topics") or []:
        topics.append(
            {
                "episode_id": episode_id,
                "show_slug": show_slug,
                "topic": topic.get("topic"),
                "score": topic.get("score"),
                "quote": first_quote(topic.get("mentions")),
            }
        )

    people: list[dict[str, Any]] = []
    for person in record.get("people") or []:
        name = person.get("name") or ""
        normalized = person.get("normalized") or normalize_person_key(name)
        people.append(
            {
                "episode_id": episode_id,
                "show_slug": show_slug,
                "person_name": name,
                "person_normalized": normalized,
                "role_hint": person.get("role_hint"),
                "mention_count": person.get("mention_count"),
                "quote": first_quote(person.get("mentions")),
            }
        )

    stances: list[dict[str, Any]] = []
    for stance in record.get("stances") or []:
        stances.append(
            {
                "episode_id": episode_id,
                "show_slug": show_slug,
                "target_type": stance.get("target_type"),
                "target": stance.get("target"),
                "stance": stance.get("stance"),
                "confidence": stance.get("confidence"),
                "quote": stance.get("quote"),
            }
        )

    claims: list[dict[str, Any]] = []
    for claim in record.get("claims") or []:
        claims.append(
            {
                "episode_id": episode_id,
                "show_slug": show_slug,
                "claim_text": claim.get("text"),
                "topics": json.dumps(claim.get("about") or []),
            }
        )

    return {
        "episodes": episodes,
        "episode_bills": bills,
        "episode_topics": topics,
        "episode_people": people,
        "episode_stances": stances,
        "episode_claims": claims,
    }


def empty_tables() -> dict[str, list[dict[str, Any]]]:
    return {name: [] for name in TABLE_FILES}


def merge_tables(
    dest: dict[str, list[dict[str, Any]]],
    src: dict[str, list[dict[str, Any]]],
) -> None:
    for name in TABLE_FILES:
        dest[name].extend(src.get(name) or [])


def write_sqlite(tables: dict[str, list[dict[str, Any]]], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(path):
        os.remove(path)

    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY,
                show_slug TEXT,
                title TEXT,
                pub_date TEXT,
                usable INTEGER,
                char_count INTEGER,
                quality_reason TEXT,
                audio_gcs_uri TEXT,
                transcript_gcs_uri TEXT,
                source_url TEXT,
                top_topics TEXT,
                bill_refs TEXT,
                people_mentioned TEXT,
                enriched_at TEXT,
                engine TEXT
            );
            CREATE TABLE episode_bills (
                episode_id TEXT,
                show_slug TEXT,
                bill_ref TEXT,
                bill_normalized TEXT,
                kind TEXT,
                quote TEXT
            );
            CREATE TABLE episode_topics (
                episode_id TEXT,
                show_slug TEXT,
                topic TEXT,
                score INTEGER,
                quote TEXT
            );
            CREATE TABLE episode_people (
                episode_id TEXT,
                show_slug TEXT,
                person_name TEXT,
                person_normalized TEXT,
                role_hint TEXT,
                mention_count INTEGER,
                quote TEXT
            );
            CREATE TABLE episode_stances (
                episode_id TEXT,
                show_slug TEXT,
                target_type TEXT,
                target TEXT,
                stance TEXT,
                confidence REAL,
                quote TEXT
            );
            CREATE TABLE episode_claims (
                episode_id TEXT,
                show_slug TEXT,
                claim_text TEXT,
                topics TEXT
            );
            CREATE INDEX idx_bills_norm ON episode_bills(bill_normalized);
            CREATE INDEX idx_topics ON episode_topics(topic);
            CREATE INDEX idx_people_norm ON episode_people(person_normalized);
            CREATE INDEX idx_stances_target ON episode_stances(target);
            CREATE INDEX idx_episodes_show ON episodes(show_slug);
            """
        )

        def insert_many(table: str, rows: list[dict[str, Any]]) -> None:
            if not rows:
                return
            cols = list(rows[0].keys())
            placeholders = ",".join("?" for _ in cols)
            col_sql = ",".join(cols)
            values = [tuple(row.get(c) for c in cols) for row in rows]
            cur.executemany(
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                values,
            )

        for table in TABLE_FILES:
            insert_many(table, tables[table])
        conn.commit()
    finally:
        conn.close()


def write_gcs_jsonl(
    tables: dict[str, list[dict[str, Any]]],
    bucket_name: str,
    client: Any,
) -> dict[str, int]:
    bucket = client.bucket(bucket_name)
    counts: dict[str, int] = {}
    for table, rows in tables.items():
        blob = bucket.blob(f"{SILVER_PREFIX}/{table}.jsonl")
        lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        if lines:
            lines += "\n"
        blob.upload_from_string(lines, content_type="application/x-ndjson")
        counts[table] = len(rows)

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "prefix": SILVER_PREFIX,
        "counts": counts,
        "engine": "silver_v1_sqlite_jsonl",
        "cost": "local_cpu_gcs_only_no_paid_apis",
    }
    bucket.blob(f"{SILVER_PREFIX}/_manifest.json").upload_from_string(
        json.dumps(manifest, indent=2),
        content_type="application/json",
    )
    return counts


def iter_enrichment_records(
    bucket: Any,
    show_slug_filter: str | None = None,
) -> Iterable[dict[str, Any]]:
    prefix = f"{ENRICHMENT_PREFIX}/"
    if show_slug_filter:
        prefix = f"{ENRICHMENT_PREFIX}/{show_slug_filter}/"

    for blob in bucket.list_blobs(prefix=prefix):
        if not blob.name.endswith(".json") or blob.name.endswith("_manifest.json"):
            continue
        try:
            yield json.loads(blob.download_as_text())
        except Exception:
            log.exception("Failed to read enrichment %s", blob.name)


def build_silver(
    local: bool = True,
    gcs: bool = True,
    show_slug_filter: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    config = load_config()
    bucket_name = config["bucket_name"]
    if not bucket_name:
        raise ValueError("Set GCP_BUCKET_NAME in .env")

    client = get_storage_client(config)
    bucket = client.bucket(bucket_name)
    tables = empty_tables()
    checked = 0
    usable = 0

    for record in iter_enrichment_records(bucket, show_slug_filter=show_slug_filter):
        checked += 1
        flat = flatten_enrichment(record)
        merge_tables(tables, flat)
        if flat["episodes"] and flat["episodes"][0].get("usable"):
            usable += 1

    stats: dict[str, Any] = {
        "checked": checked,
        "usable": usable,
        "counts": {name: len(tables[name]) for name in TABLE_FILES},
        "sqlite_path": None,
        "gcs_prefix": None,
    }

    if local:
        path = sqlite_path or SQLITE_PATH
        write_sqlite(tables, path)
        stats["sqlite_path"] = path
        log.info("Wrote SQLite silver DB → %s", path)

    if gcs:
        write_gcs_jsonl(tables, bucket_name, client)
        stats["gcs_prefix"] = f"gs://{bucket_name}/{SILVER_PREFIX}/"
        log.info("Wrote GCS silver JSONL → %s", stats["gcs_prefix"])

    log.info(
        "Silver build done. checked=%s usable=%s counts=%s",
        checked,
        usable,
        stats["counts"],
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build free silver tables (SQLite + GCS JSONL) from enrichment",
    )
    parser.add_argument("--show", type=str, default=None, help="Only one show slug")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Write SQLite only (no GCS upload)",
    )
    parser.add_argument(
        "--gcs-only",
        action="store_true",
        help="Write GCS JSONL only (no local SQLite)",
    )
    parser.add_argument(
        "--sqlite-path",
        type=str,
        default=None,
        help="Override local SQLite path",
    )
    args = parser.parse_args()
    if args.local_only and args.gcs_only:
        parser.error("Use only one of --local-only / --gcs-only")

    local = not args.gcs_only
    gcs = not args.local_only
    build_silver(
        local=local,
        gcs=gcs,
        show_slug_filter=args.show,
        sqlite_path=args.sqlite_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
