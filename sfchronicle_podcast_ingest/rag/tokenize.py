"""Cheap tokenization for keyword overlap (no embeddings)."""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    """
    a an the and or but if in on at to for of from with by as is are was were be
    been being it its this that these those i you he she we they them my your
    our their what which who whom how when where why about into over after
    before between through during under again further then once here there all
    any both each few more most other some such no nor not only own same so
    than too very can will just don should now also said say says saying
    podcast episode show about something things thing really going get got
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    tokens = set()
    for match in _TOKEN_RE.finditer(text.lower()):
        tok = match.group(0)
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        tokens.add(tok)
        # Keep digit-heavy file numbers even if short (e.g. keep 250823 fully).
    return tokens


def tokenize_many(parts: list[str | None]) -> set[str]:
    out: set[str] = set()
    for part in parts:
        out |= tokenize(part)
    return out
