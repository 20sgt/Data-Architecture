"""Word-overlap retrieval: NL query + recent gold matters → top podcast chunks."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from .chunks import Chunk, load_chunks
from .matters import Matter, filter_recent_matters, load_matters
from .tokenize import tokenize, tokenize_many


def _parse_as_of(value: str | date | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def score_chunk(chunk: Chunk, query_tokens: set[str], matter_tokens: set[str]) -> float:
    """Score with query overlap required when the query has content tokens."""
    chunk_tokens = tokenize(chunk.search_text())
    q_hit = query_tokens & chunk_tokens
    m_hit = matter_tokens & chunk_tokens

    if query_tokens and not q_hit:
        # Query present but no query words in chunk → skip (avoids matter-title noise).
        return 0.0
    if not q_hit and not m_hit:
        return 0.0

    score = 0.0
    for tok in q_hit:
        score += 3.0 if tok.isdigit() and len(tok) >= 5 else 2.0
    for tok in m_hit:
        score += 3.0 if tok.isdigit() and len(tok) >= 5 else 1.0
    score += min(len(chunk.quote), 400) / 2000.0
    return score


def retrieve(
    query: str,
    *,
    top_k: int = 3,
    window_days: int = 28,
    as_of: str | date | None = None,
    matters_path: str | Path | None = None,
    sqlite_path: str | Path | None = None,
    chunks: list[Chunk] | None = None,
    matters: list[Matter] | None = None,
) -> dict[str, Any]:
    """
    Return JSON-serializable retrieval result.

    status:
      - ok: top_k chunks with word overlap against query + recent matters
      - no_recent_data: no matters in window, or no matching podcast chunks
    """
    as_of_date = _parse_as_of(as_of)
    all_matters = matters if matters is not None else load_matters(matters_path)
    recent = filter_recent_matters(
        all_matters, window_days=window_days, as_of=as_of_date
    )

    base: dict[str, Any] = {
        "query": query,
        "window_days": window_days,
        "as_of": as_of_date.isoformat(),
        "matters_considered": [m.to_public_dict() for m in recent],
        "status": "no_recent_data",
        "chunks": [],
        "message": None,
    }

    if not recent:
        base["message"] = (
            f"No gold matters in the last {window_days} days "
            f"(as of {as_of_date.isoformat()}). Nothing to ground podcast search."
        )
        return base

    query_tokens = tokenize(query)
    matter_tokens = tokenize_many([m.search_text() for m in recent])

    pool = chunks if chunks is not None else load_chunks(sqlite_path)
    scored: list[tuple[float, Chunk]] = []
    for chunk in pool:
        s = score_chunk(chunk, query_tokens, matter_tokens)
        if s > 0:
            scored.append((s, chunk))

    scored.sort(key=lambda item: (-item[0], item[1].episode_id, item[1].kind))

    picked: list[tuple[float, Chunk]] = []
    seen_episodes: set[str] = set()
    for score, chunk in scored:
        if chunk.episode_id in seen_episodes:
            continue
        picked.append((score, chunk))
        seen_episodes.add(chunk.episode_id)
        if len(picked) >= top_k:
            break

    if not picked:
        base["message"] = (
            f"Found {len(recent)} recent matter(s) but no podcast chunks with "
            "matching words. Return no_recent_data to the LLM."
        )
        return base

    base["status"] = "ok"
    base["chunks"] = [chunk.to_public_dict(round(score, 3)) for score, chunk in picked]
    base["message"] = f"Returning {len(picked)} related podcast chunk(s)."
    return base
