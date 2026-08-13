"""Load Databricks gold matter exports and filter to a recent window."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_MATTERS_PATH = Path(__file__).resolve().parent / "data" / "matters.json"
EXAMPLE_MATTERS_PATH = Path(__file__).resolve().parent / "data" / "matters.example.json"

DATE_FIELDS = (
    "introduced_date",
    "first_committee_date",
    "final_action_date",
    "enactment_date",
)


@dataclass(frozen=True)
class Matter:
    matter_file: str
    matter_name: str
    matter_title: str = ""
    matter_type: str = ""
    matter_sk: str = ""
    matter_id: str = ""
    status: str = ""
    lifecycle: str = ""
    aliases: tuple[str, ...] = ()
    dates: tuple[date, ...] = ()

    def search_text(self) -> str:
        # Prefer short identifiers; full matter_title is too noisy for word overlap.
        parts = [
            self.matter_file,
            self.matter_name,
            self.matter_type,
            *self.aliases,
        ]
        return " ".join(p for p in parts if p)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "matter_file": self.matter_file,
            "matter_name": self.matter_name,
            "matter_type": self.matter_type or None,
            "status": self.status or None,
            "matter_sk": self.matter_sk or None,
        }


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _matter_dates(row: dict[str, Any]) -> tuple[date, ...]:
    found: list[date] = []
    for field in DATE_FIELDS:
        parsed = _parse_date(row.get(field))
        if parsed:
            found.append(parsed)
    # Optional single "activity_date" from a custom export.
    extra = _parse_date(row.get("activity_date"))
    if extra:
        found.append(extra)
    return tuple(sorted(set(found)))


def load_matters(path: str | Path | None = None) -> list[Matter]:
    """Load matters JSON list. Falls back to example file if default missing."""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    env = os.getenv("RAG_MATTERS_PATH")
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_MATTERS_PATH)
    candidates.append(EXAMPLE_MATTERS_PATH)

    chosen: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            chosen = candidate
            break
    if chosen is None:
        return []

    with open(chosen, encoding="utf-8") as fh:
        raw = json.load(fh)

    rows = raw if isinstance(raw, list) else raw.get("matters") or []
    matters: list[Matter] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        matter_file = str(row.get("matter_file") or row.get("file_number") or "").strip()
        matter_name = str(row.get("matter_name") or row.get("name") or "").strip()
        if not matter_file and not matter_name:
            continue
        aliases = row.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        matters.append(
            Matter(
                matter_file=matter_file,
                matter_name=matter_name,
                matter_title=str(row.get("matter_title") or row.get("title") or ""),
                matter_type=str(row.get("matter_type") or row.get("type") or ""),
                matter_sk=str(row.get("matter_sk") or ""),
                matter_id=str(row.get("matter_id") or ""),
                status=str(row.get("status") or ""),
                lifecycle=str(row.get("lifecycle") or ""),
                aliases=tuple(str(a) for a in aliases if a),
                dates=_matter_dates(row),
            )
        )
    return matters


def filter_recent_matters(
    matters: list[Matter],
    *,
    window_days: int = 28,
    as_of: date | None = None,
) -> list[Matter]:
    """Keep matters with any tracked date inside [as_of - window, as_of]."""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=window_days)
    recent: list[Matter] = []
    for matter in matters:
        if not matter.dates:
            # No dates in export → exclude from "recent" window (strict).
            continue
        if any(start <= d <= as_of for d in matter.dates):
            recent.append(matter)
    return recent
