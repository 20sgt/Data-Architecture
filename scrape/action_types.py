"""Shared raw-label -> canonical action-type mapping (the cross-slice contract).

`action_type_code` participates in the fact_matter_action dedup natural key
`(matter_id, meeting_id, action_type_code)`. BOTH scrapers (scrape-by-meeting and
scrape-by-legislation) MUST map raw Legistar action labels to codes through THIS module,
or the same physical action reached from two crawl paths produces duplicate fact rows
under different codes (the silent-duplicate trap — see erd/ERD.md §2).

Design:
  * The raw label is ALWAYS preserved verbatim downstream (silver `action_raw`,
    gold `fact_matter_action.action_text`). Normalization can be re-derived as the
    mapping evolves, so OTHER is safe — never lossy.
  * Matching is deterministic substring rules over a punctuation-stripped, upper-cased
    label, in priority order (most specific first). No LLM.

Verified raw labels (live, 2026-06-21): ADOPTED, FINALLY PASSED, APPROVED, RECOMMENDED,
CONTINUED, and BOTH "PASSED, ON FIRST READING" and "PASSED ON FIRST READING" (the comma
variant is exactly why this normalization is mandatory).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# action_category vocabulary (matches dim_action_type.action_category in the ERD):
#   introduction / referral / committee / board / amendment / disposition / other
CAT_INTRODUCTION = "introduction"
CAT_REFERRAL = "referral"
CAT_COMMITTEE = "committee"
CAT_BOARD = "board"
CAT_AMENDMENT = "amendment"
CAT_DISPOSITION = "disposition"
CAT_OTHER = "other"


@dataclass(frozen=True)
class ActionType:
    code: str
    category: str


# Canonical lookup rows -> seed dim_action_type. This list is the source of truth for the
# action_type_code vocabulary and is kept in lockstep with erd/schema.dbml's dim_action_type note.
# PASSED_COMMITTEE is included for ERD parity even though the meeting scrape maps committee
# actions to RECOMMENDED (a committee body never emits a "PASSED" label — it "RECOMMENDED"s);
# the legislation slice may emit it from a matter-side label.
DIM_ACTION_TYPE_SEED: list[tuple[str, str, str]] = [
    ("INTRODUCED", CAT_INTRODUCTION, "Matter introduced / received and assigned"),
    ("REFERRED", CAT_REFERRAL, "Referred to a committee or department"),
    ("RECOMMENDED", CAT_COMMITTEE, "Committee recommended the matter (incl. as amended / as committee report)"),
    ("PASSED_COMMITTEE", CAT_COMMITTEE, "Matter passed at committee (matter-side label; meeting scrape uses RECOMMENDED)"),
    ("CONTINUED", CAT_COMMITTEE, "Hearing continued to a future date or call of the chair"),
    ("PASSED_BOARD_1ST_READING", CAT_BOARD, "Board passed an ordinance on first reading"),
    ("PASSED_BOARD_2ND_READING", CAT_BOARD, "Board finally passed an ordinance (second/final reading)"),
    ("ADOPTED", CAT_DISPOSITION, "Resolution/motion adopted (incl. adopted as amended)"),
    ("APPROVED", CAT_DISPOSITION, "Matter approved (incl. approved as amended)"),
    ("AMENDED", CAT_AMENDMENT, "Amendment is the sole action (incl. amendment of the whole)"),
    ("WITHDRAWN", CAT_DISPOSITION, "Matter withdrawn"),
    ("TABLED", CAT_DISPOSITION, "Matter tabled"),
    ("FILED", CAT_DISPOSITION, "Matter filed / closed without passage"),
    ("OTHER", CAT_OTHER, "Unmapped raw label — preserved verbatim in action_text"),
]

OTHER = ActionType("OTHER", CAT_OTHER)

# Canonical roll-call vote values (erd/schema.dbml fact_vote.vote_value CHECK). The site emits
# "No"; normalize to "Nay". Excused/Absent/Recused casing still unconfirmed against a live
# absentee meeting — confirm before locking the CHECK.
_VOTE_MAP = {"AYE": "Aye", "YES": "Aye", "NO": "Nay", "NAY": "Nay",
             "EXCUSED": "Excused", "ABSENT": "Absent", "RECUSED": "Recused", "PRESENT": "Present"}

_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def _canon(raw: str) -> str:
    """Upper-case, strip punctuation (drops the 'PASSED, ON…' comma), collapse whitespace."""
    s = _PUNCT.sub(" ", (raw or "").upper())
    return _WS.sub(" ", s).strip()


def normalize_action(raw: str | None) -> ActionType:
    """Map a raw Legistar action label to a canonical (code, category).

    Rules are evaluated most-specific first. Unknown labels -> OTHER (the raw label is
    still preserved verbatim by callers, so OTHER is recoverable, never lossy).
    """
    s = _canon(raw)
    if not s:
        return OTHER

    # --- board final passage (check before generic PASSED / FIRST READING) ---
    if "FINALLY PASSED" in s or "SECOND READING" in s or "FINAL PASSAGE" in s:
        return ActionType("PASSED_BOARD_2ND_READING", CAT_BOARD)
    if "FIRST READING" in s or ("PASSED" in s and "READING" in s):
        return ActionType("PASSED_BOARD_1ST_READING", CAT_BOARD)

    # --- committee actions ---
    if "RECOMMENDED" in s:                       # incl. "RECOMMENDED AS AMENDED" / "AS COMMITTEE REPORT"
        return ActionType("RECOMMENDED", CAT_COMMITTEE)
    if "CONTINUED" in s:                         # incl. "CONTINUED TO CALL OF THE CHAIR"
        return ActionType("CONTINUED", CAT_COMMITTEE)

    # --- dispositions (BEFORE the bare AMENDED rule, so "ADOPTED AS AMENDED" -> ADOPTED, not
    #     AMENDED — the disposition is the primary action; "AS AMENDED" is a modifier) ---
    if "ADOPTED" in s:
        return ActionType("ADOPTED", CAT_DISPOSITION)
    if "APPROVED" in s:
        return ActionType("APPROVED", CAT_DISPOSITION)
    if "WITHDRAWN" in s:
        return ActionType("WITHDRAWN", CAT_DISPOSITION)
    if "TABLED" in s:
        return ActionType("TABLED", CAT_DISPOSITION)
    if "FILED" in s:
        return ActionType("FILED", CAT_DISPOSITION)

    # --- intake / routing ---
    if "REFERRED" in s or "REFER " in s or s == "REFER":
        return ActionType("REFERRED", CAT_REFERRAL)
    if "INTRODUCED" in s or "RECEIVED AND ASSIGNED" in s or "ASSIGNED" in s:
        return ActionType("INTRODUCED", CAT_INTRODUCTION)

    # --- amendment as the SOLE action (any "X AS AMENDED" already returned above) ---
    if "AMENDED" in s or "AMENDMENT" in s:
        return ActionType("AMENDED", CAT_AMENDMENT)

    return OTHER


def normalize_vote(raw: str | None) -> str | None:
    """Map a raw roll-call literal to the canonical vote_value (e.g. 'No' -> 'Nay').

    Shared by both slices so fact_vote.vote_value is consistent. Unknown non-empty literals are
    returned title-cased verbatim (never dropped) — the caller should surface them for review.
    """
    if not raw or not raw.strip():
        return None
    key = raw.strip().upper()
    return _VOTE_MAP.get(key, raw.strip().title())
