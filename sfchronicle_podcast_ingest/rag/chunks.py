"""Build citation chunks from podcast silver SQLite (quotes + title + URL)."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SQLITE = Path(__file__).resolve().parents[1] / "data" / "podcast_silver.sqlite"


@dataclass(frozen=True)
class Chunk:
    episode_id: str
    title: str
    url: str
    quote: str
    kind: str
    label: str = ""
    pub_date: str = ""
    show_slug: str = ""

    def search_text(self) -> str:
        return " ".join(
            part for part in (self.title, self.quote, self.label, self.kind) if part
        )

    def to_public_dict(self, score: float) -> dict[str, Any]:
        return {
            "title": self.title,
            "quote": self.quote,
            "url": self.url,
            "score": score,
            "episode_id": self.episode_id,
            "kind": self.kind,
            "label": self.label or None,
            "pub_date": self.pub_date or None,
            "show_slug": self.show_slug or None,
        }


def resolve_sqlite_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env = os.getenv("SILVER_SQLITE_PATH")
    if env:
        return Path(env)
    return DEFAULT_SQLITE


def load_chunks(sqlite_path: str | Path | None = None) -> list[Chunk]:
    path = resolve_sqlite_path(sqlite_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing silver DB at {path}. Build it with ./run_silver.sh"
        )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        chunks: list[Chunk] = []
        chunks.extend(_from_bills(conn))
        chunks.extend(_from_topics(conn))
        chunks.extend(_from_people(conn))
        chunks.extend(_from_claims(conn))
        return chunks
    finally:
        conn.close()


def _row_chunk(
    row: sqlite3.Row,
    *,
    quote: str,
    kind: str,
    label: str,
) -> Chunk | None:
    quote = (quote or "").strip()
    if len(quote) < 20:
        return None
    title = (row["title"] or "").strip() or row["episode_id"]
    url = (row["source_url"] or "").strip()
    if not url:
        return None
    return Chunk(
        episode_id=row["episode_id"],
        title=title,
        url=url,
        quote=quote,
        kind=kind,
        label=label,
        pub_date=row["pub_date"] or "",
        show_slug=row["show_slug"] or "",
    )


def _from_bills(conn: sqlite3.Connection) -> list[Chunk]:
    sql = """
        SELECT e.episode_id, e.title, e.source_url, e.pub_date, e.show_slug,
               b.bill_ref AS label, b.quote AS quote
        FROM episode_bills b
        JOIN episodes e USING (episode_id)
        WHERE e.usable = 1
          AND IFNULL(e.source_url, '') != ''
          AND IFNULL(b.quote, '') != ''
    """
    out: list[Chunk] = []
    for row in conn.execute(sql):
        chunk = _row_chunk(row, quote=row["quote"], kind="bill", label=row["label"] or "")
        if chunk:
            out.append(chunk)
    return out


def _from_topics(conn: sqlite3.Connection) -> list[Chunk]:
    sql = """
        SELECT e.episode_id, e.title, e.source_url, e.pub_date, e.show_slug,
               t.topic AS label, t.quote AS quote
        FROM episode_topics t
        JOIN episodes e USING (episode_id)
        WHERE e.usable = 1
          AND IFNULL(e.source_url, '') != ''
          AND IFNULL(t.quote, '') != ''
    """
    out: list[Chunk] = []
    for row in conn.execute(sql):
        chunk = _row_chunk(row, quote=row["quote"], kind="topic", label=row["label"] or "")
        if chunk:
            out.append(chunk)
    return out


def _from_people(conn: sqlite3.Connection) -> list[Chunk]:
    sql = """
        SELECT e.episode_id, e.title, e.source_url, e.pub_date, e.show_slug,
               p.person_name AS label, p.quote AS quote
        FROM episode_people p
        JOIN episodes e USING (episode_id)
        WHERE e.usable = 1
          AND IFNULL(e.source_url, '') != ''
          AND IFNULL(p.quote, '') != ''
    """
    out: list[Chunk] = []
    for row in conn.execute(sql):
        chunk = _row_chunk(row, quote=row["quote"], kind="person", label=row["label"] or "")
        if chunk:
            out.append(chunk)
    return out


def _from_claims(conn: sqlite3.Connection) -> list[Chunk]:
    sql = """
        SELECT e.episode_id, e.title, e.source_url, e.pub_date, e.show_slug,
               '' AS label, c.claim_text AS quote
        FROM episode_claims c
        JOIN episodes e USING (episode_id)
        WHERE e.usable = 1
          AND IFNULL(e.source_url, '') != ''
          AND IFNULL(c.claim_text, '') != ''
    """
    out: list[Chunk] = []
    for row in conn.execute(sql):
        chunk = _row_chunk(row, quote=row["quote"], kind="claim", label="")
        if chunk:
            out.append(chunk)
    return out
