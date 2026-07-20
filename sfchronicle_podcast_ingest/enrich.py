#!/usr/bin/env python3
"""
Enrich podcast transcripts into queryable civic entities.

Extracts (locally, no paid LLM/API):
  - bill / proposition / ordinance references
  - people / officials mentioned
  - civic topics
  - stance/sentiment cues toward bills or topics
  - short quote windows for citations

Usage:
  ./.venv/bin/python3 enrich.py
  ./.venv/bin/python3 enrich.py --limit 10
  ./.venv/bin/python3 enrich.py --force
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from ingest import get_storage_client, load_config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

TRANSCRIPT_PREFIX = os.getenv("TRANSCRIPT_PREFIX", "podcasts/transcripts_whisper")
# Legacy mixed STT/Whisper transcripts (undisturbed): podcasts/transcripts
METADATA_PREFIX = "podcasts/metadata"
ENRICHMENT_PREFIX = "podcasts/enrichment"
MIN_USABLE_CHARS = int(os.getenv("ENRICH_MIN_CHARS", "800"))

# Patterns for California / SF legislative and ballot references.
BILL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("state_bill", re.compile(r"\b(?:Assembly|Senate)\s+Bill\s+(\d+[A-Z]?)\b", re.I)),
    ("state_bill_short", re.compile(r"\b(A\.?B\.?|S\.?B\.?)\s*(\d+[A-Z]?)\b", re.I)),
    ("proposition", re.compile(r"\b(?:Proposition|Prop\.?)\s+([A-Z0-9]{1,3})\b", re.I)),
    ("measure", re.compile(r"\bMeasure\s+([A-Z0-9]{1,3})\b", re.I)),
    ("ordinance", re.compile(r"\bOrdinance\s+(No\.?\s*)?(\d{2,4}[-–]?\d*)\b", re.I)),
    ("board_file", re.compile(r"\b(?:File|Resolution)\s+(No\.?\s*)?(\d{2,4}[-–]\d+)\b", re.I)),
    ("house_senate_bill", re.compile(r"\b([HS]\.?R\.?\s*\d+)\b", re.I)),
]

# Civic topic lexicon: topic -> keywords/phrases.
TOPIC_LEXICON: dict[str, list[str]] = {
    "homelessness": [
        "homeless", "homelessness", "unhoused", "encampment", "shelter", "rv residents",
        "affordable housing crisis",
    ],
    "housing": [
        "housing", "rent", "landlord", "tenant", "eviction", "zoning", "affordable housing",
        "apartment", "condo", "development",
    ],
    "public_safety": [
        "crime", "theft", "police", "public safety", "robbery", "assault", "shooting",
        "district attorney", "prosecut",
    ],
    "policing": [
        "police", "sfpd", "officer", "use of force", "defund", "police budget",
    ],
    "transit": [
        "bart", "muni", "transit", "bus", "caltrain", "fare", "subway", "commute",
    ],
    "covid": [
        "covid", "coronavirus", "pandemic", "shelter-in-place", "vaccine", "mask",
        "shutdown", "lockdown",
    ],
    "budget": [
        "budget", "deficit", "tax", "spending", "funding", "fiscal", "billion",
    ],
    "education": [
        "school", "sfusd", "teacher", "student", "classroom", "education",
    ],
    "environment": [
        "climate", "environment", "emission", "wildfire", "drought", "sea level",
        "pollution",
    ],
    "immigration": [
        "immigration", "immigrant", "asylum", "ice", "border", "deport",
    ],
    "healthcare": [
        "hospital", "health care", "healthcare", "clinic", "mental health", "overdose",
        "fentanyl", "opioid",
    ],
    "economy": [
        "economy", "jobs", "unemployment", "downtown", "office", "retail", "business",
        "layoff",
    ],
    "elections": [
        "election", "ballot", "voter", "campaign", "candidate", "primary", "mayor",
        "supervisor",
    ],
    "drugs": [
        "fentanyl", "overdose", "drug", "narcotic", "addiction", "tenderloin",
    ],
}

# Fallback people lexicon if data/representatives.json is missing.
_FALLBACK_PEOPLE: dict[str, str] = {
    "daniel lurie": "mayor",
    "london breed": "former_mayor",
    "scott wiener": "state_senator",
    "nancy pelosi": "representative",
    "gavin newsom": "governor",
    "cecilia lei": "host",
    "heather knight": "columnist",
    "audrey cooper": "editor",
    "phil matier": "columnist",
    "annie vainshtein": "reporter",
    "monica gandhi": "doctor",
    "john rothmann": "host",
    "solei ho": "critic",
    "soleil ho": "critic",
    "demian bulwa": "editor",
    "matt kawahara": "reporter",
    "susan slusser": "reporter",
    "john shea": "reporter",
    "peter hartlaub": "critic",
    "laura wenus": "host",
}

# name_lower -> role; also PERSON_NORMALIZED: name_lower -> stable id for joins
KNOWN_PEOPLE: dict[str, str] = {}
PERSON_NORMALIZED: dict[str, str] = {}


def load_representatives(path: str | None = None) -> None:
    """Load people/representative lexicon from JSON (no paid APIs)."""
    KNOWN_PEOPLE.clear()
    PERSON_NORMALIZED.clear()
    KNOWN_PEOPLE.update(_FALLBACK_PEOPLE)
    for name, role in _FALLBACK_PEOPLE.items():
        PERSON_NORMALIZED[name] = re.sub(r"[^a-z0-9]+", "_", name).strip("_")

    candidates = []
    if path:
        candidates.append(path)
    env_path = os.getenv("REPRESENTATIVES_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append(
        os.path.join(os.path.dirname(__file__), "data", "representatives.json")
    )

    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        with open(candidate, encoding="utf-8") as fh:
            rows = json.load(fh)
        for row in rows:
            name = (row.get("name") or "").strip().lower()
            if not name:
                continue
            role = (row.get("role") or "unknown").strip()
            normalized = (row.get("normalized") or "").strip()
            if not normalized:
                normalized = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
            KNOWN_PEOPLE[name] = role
            PERSON_NORMALIZED[name] = normalized
        log.info("Loaded %d people from %s", len(rows), candidate)
        return
    log.info("Using fallback people lexicon (%d names)", len(KNOWN_PEOPLE))


load_representatives()

SUPPORT_CUES = [
    "support", "supports", "supporting", "backed", "backs", "endorse", "endorsed",
    "in favor", "voted for", "applaud", "welcome", "necessary", "should pass",
    "good idea", "strong support",
]
OPPOSE_CUES = [
    "oppose", "opposes", "opposing", "against", "voted against", "reject", "rejected",
    "criticize", "criticized", "condemn", "block", "blocked", "too strict", "over the top",
    "bad idea", "should not", "shouldn't",
]
CONCERN_CUES = [
    "concern", "worried", "crisis", "struggle", "struggling", "problem", "challenge",
    "anxious", "fear", "urgent", "ballooned", "severe",
]

PERSON_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b"
)
GARBAGE_TOKEN_RE = re.compile(r"\b(?:here|you|oh|yeah|hmm|mmm|ah+)\b", re.I)

# Reject place/show/org names that look like people in ASR text.
PERSON_BLOCKLIST = {
    "san francisco",
    "san diego",
    "san jose",
    "los angeles",
    "marin county",
    "bay area",
    "united states",
    "fifth emission",
    "fifth mission",
    "new york",
    "board of",
    "city hall",
    "white house",
}


@dataclass
class Mention:
    quote: str
    start_char: int
    end_char: int


@dataclass
class BillMention:
    ref: str
    kind: str
    normalized: str
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class PersonMention:
    name: str
    role_hint: str | None
    mention_count: int
    normalized: str | None = None
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class TopicMention:
    topic: str
    score: int
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class Stance:
    target_type: str  # bill | topic | person
    target: str
    stance: str  # supports | opposes | concerned | neutral
    quote: str
    confidence: float


def enrichment_blob_path(show_slug: str, episode_id: str) -> str:
    return f"{ENRICHMENT_PREFIX}/{show_slug}/{episode_id}.json"


def metadata_blob_path(show_slug: str, episode_id: str) -> str:
    return f"{METADATA_PREFIX}/{show_slug}/{episode_id}.json"


def parse_transcript_path(blob_name: str) -> tuple[str, str] | None:
    # podcasts/transcripts_whisper/{show}/{episode_id}.json  (preferred)
    # podcasts/transcripts/{show}/{episode_id}.json          (legacy)
    parts = blob_name.split("/")
    if len(parts) != 4 or parts[0] != "podcasts":
        return None
    if parts[1] not in {"transcripts_whisper", "transcripts"}:
        return None
    show_slug = parts[2]
    episode_id = parts[3].removesuffix(".json")
    if not show_slug or not episode_id:
        return None
    return show_slug, episode_id


def window_around(text: str, start: int, end: int, radius: int = 120) -> Mention:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    quote = re.sub(r"\s+", " ", text[left:right]).strip()
    return Mention(quote=quote, start_char=start, end_char=end)


def assess_quality(transcript: str) -> dict[str, Any]:
    cleaned = transcript.strip()
    char_count = len(cleaned)
    if char_count < MIN_USABLE_CHARS:
        return {
            "usable": False,
            "char_count": char_count,
            "reason": "transcript_too_short",
        }

    tokens = re.findall(r"[A-Za-z']+", cleaned.lower())
    if not tokens:
        return {"usable": False, "char_count": char_count, "reason": "no_tokens"}

    garbage_ratio = sum(1 for t in tokens if GARBAGE_TOKEN_RE.fullmatch(t)) / len(tokens)
    unique_ratio = len(set(tokens)) / len(tokens)
    if garbage_ratio > 0.35 or unique_ratio < 0.12:
        return {
            "usable": False,
            "char_count": char_count,
            "garbage_ratio": round(garbage_ratio, 3),
            "unique_ratio": round(unique_ratio, 3),
            "reason": "low_quality_asr",
        }

    return {
        "usable": True,
        "char_count": char_count,
        "garbage_ratio": round(garbage_ratio, 3),
        "unique_ratio": round(unique_ratio, 3),
        "reason": None,
    }


def normalize_bill_ref(kind: str, match: re.Match[str]) -> tuple[str, str]:
    if kind == "state_bill":
        # Assembly Bill 123 / Senate Bill 45
        prefix = "AB" if match.group(0).lower().startswith("assembly") else "SB"
        num = match.group(1).upper()
        ref = f"{prefix} {num}"
        return ref, f"{prefix.lower()}_{num.lower()}"
    if kind == "state_bill_short":
        prefix = match.group(1).upper().replace(".", "")
        num = match.group(2).upper()
        ref = f"{prefix} {num}"
        return ref, f"{prefix.lower()}_{num.lower()}"
    if kind in {"proposition", "measure"}:
        value = match.group(1).upper()
        label = "Prop" if kind == "proposition" else "Measure"
        ref = f"{label} {value}"
        return ref, f"{label.lower()}_{value.lower()}"
    if kind == "ordinance":
        num = match.group(2)
        ref = f"Ordinance {num}"
        return ref, f"ordinance_{num.lower()}"
    if kind == "board_file":
        num = match.group(2)
        ref = f"File {num}"
        return ref, f"file_{num.lower()}"
    ref = match.group(0).strip()
    return ref, re.sub(r"[^a-z0-9]+", "_", ref.lower()).strip("_")


def extract_bills(text: str) -> list[BillMention]:
    found: dict[str, BillMention] = {}
    for kind, pattern in BILL_PATTERNS:
        for match in pattern.finditer(text):
            ref, normalized = normalize_bill_ref(kind, match)
            mention = window_around(text, match.start(), match.end())
            if normalized not in found:
                found[normalized] = BillMention(
                    ref=ref,
                    kind=kind,
                    normalized=normalized,
                    mentions=[mention],
                )
            elif len(found[normalized].mentions) < 5:
                found[normalized].mentions.append(mention)
    return sorted(found.values(), key=lambda b: b.ref.lower())


def extract_topics(text: str) -> list[TopicMention]:
    lower = text.lower()
    topics: list[TopicMention] = []
    for topic, keywords in TOPIC_LEXICON.items():
        score = 0
        mentions: list[Mention] = []
        for keyword in keywords:
            # Word boundaries avoid "ice" matching inside "police"/"office".
            pattern = re.compile(rf"\b{re.escape(keyword)}\b")
            for match in pattern.finditer(lower):
                score += 1
                if len(mentions) < 5:
                    mentions.append(window_around(text, match.start(), match.end()))
        if score > 0:
            topics.append(TopicMention(topic=topic, score=score, mentions=mentions))
    topics.sort(key=lambda t: (-t.score, t.topic))
    return topics


def extract_people(text: str, title: str = "", description: str = "") -> list[PersonMention]:
    combined = f"{title}\n{description}\n{text}"
    lower = combined.lower()
    people: dict[str, PersonMention] = {}

    for name, role in KNOWN_PEOPLE.items():
        for match in re.finditer(re.escape(name), lower):
            display = " ".join(part.capitalize() for part in name.split())
            key = PERSON_NORMALIZED.get(name, name)
            mention = window_around(combined, match.start(), match.end())
            if key not in people:
                people[key] = PersonMention(
                    name=display,
                    role_hint=role,
                    mention_count=1,
                    normalized=key,
                    mentions=[mention],
                )
            else:
                people[key].mention_count += 1
                if len(people[key].mentions) < 5:
                    people[key].mentions.append(mention)

    # Generic capitalized names near political verbs/nouns.
    context_words = (
        "said", "says", "mayor", "supervisor", "senator", "governor", "reporter",
        "host", "voted", "supports", "opposes", "argued", "explained",
    )
    for match in PERSON_PATTERN.finditer(text):
        name = match.group(1)
        # Filter common false positives.
        if name.split()[0] in {"The", "This", "That", "And", "For", "With", "From", "New"}:
            continue
        if len(name.split()) < 2:
            continue
        key = name.lower()
        if key in PERSON_BLOCKLIST or any(key.startswith(b) for b in PERSON_BLOCKLIST):
            continue
        window = text[max(0, match.start() - 40): match.end() + 40].lower()
        if not any(word in window for word in context_words):
            continue
        if key in KNOWN_PEOPLE:
            continue
        mention = window_around(text, match.start(), match.end())
        if key not in people:
            people[key] = PersonMention(
                name=name,
                role_hint=None,
                mention_count=1,
                normalized=re.sub(r"[^a-z0-9]+", "_", key).strip("_"),
                mentions=[mention],
            )
        else:
            people[key].mention_count += 1
            if len(people[key].mentions) < 3:
                people[key].mentions.append(mention)

    ranked = sorted(people.values(), key=lambda p: (-p.mention_count, p.name.lower()))
    return ranked[:25]


def detect_stance_for_target(
    text: str,
    target: str,
    target_type: str,
) -> Stance | None:
    lower = text.lower()
    target_lower = target.lower()
    idx = lower.find(target_lower)
    if idx < 0:
        # try first token of normalized topic
        idx = lower.find(target_lower.replace("_", " "))
        if idx < 0:
            return None

    window = lower[max(0, idx - 160): idx + len(target_lower) + 160]
    support_hits = sum(1 for cue in SUPPORT_CUES if cue in window)
    oppose_hits = sum(1 for cue in OPPOSE_CUES if cue in window)
    concern_hits = sum(1 for cue in CONCERN_CUES if cue in window)

    if support_hits == oppose_hits == concern_hits == 0:
        return Stance(
            target_type=target_type,
            target=target,
            stance="neutral",
            quote=re.sub(r"\s+", " ", text[max(0, idx - 120): idx + 120]).strip(),
            confidence=0.2,
        )

    if oppose_hits > support_hits and oppose_hits >= concern_hits:
        stance = "opposes"
        conf = min(0.9, 0.35 + 0.15 * oppose_hits)
    elif support_hits > oppose_hits and support_hits >= concern_hits:
        stance = "supports"
        conf = min(0.9, 0.35 + 0.15 * support_hits)
    else:
        stance = "concerned"
        conf = min(0.85, 0.3 + 0.1 * concern_hits)

    return Stance(
        target_type=target_type,
        target=target,
        stance=stance,
        quote=re.sub(r"\s+", " ", text[max(0, idx - 120): idx + 120]).strip(),
        confidence=round(conf, 2),
    )


def extract_stances(
    text: str,
    bills: list[BillMention],
    topics: list[TopicMention],
) -> list[Stance]:
    stances: list[Stance] = []
    for bill in bills[:10]:
        stance = detect_stance_for_target(text, bill.ref, "bill")
        if stance:
            stances.append(stance)
    for topic in topics[:8]:
        # Prefer human-readable topic phrase for matching.
        phrase = topic.topic.replace("_", " ")
        stance = detect_stance_for_target(text, phrase, "topic")
        if stance:
            stances.append(stance)
    return stances


def extract_claims(text: str, topics: list[TopicMention]) -> list[dict[str, Any]]:
    """Pull sentence-like claims that mention a top topic."""
    if not topics:
        return []
    top_topics = {t.topic for t in topics[:5]}
    sentences = re.split(r"(?<=[.!?])\s+", text)
    claims: list[dict[str, Any]] = []
    for sentence in sentences:
        s = sentence.strip()
        if len(s) < 60 or len(s) > 320:
            continue
        lower = s.lower()
        matched = [
            topic for topic in top_topics
            if topic.replace("_", " ") in lower
            or any(kw in lower for kw in TOPIC_LEXICON.get(topic, [])[:4])
        ]
        if not matched:
            continue
        # Prefer sentences with judgment/evidence verbs.
        if not re.search(
            r"\b(said|says|argues|thinks|believes|shows|found|faces|needs|should|crisis|vote|voted)\b",
            lower,
        ):
            continue
        claims.append({"text": s, "about": matched[:3]})
        if len(claims) >= 12:
            break
    return claims


def enrich_episode(
    show_slug: str,
    episode_id: str,
    transcript_record: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    transcript = transcript_record.get("transcript") or ""
    title = metadata.get("title") or ""
    description = metadata.get("description") or ""
    quality = assess_quality(transcript)

    if not quality["usable"]:
        return {
            "episode_id": episode_id,
            "show_slug": show_slug,
            "title": title,
            "pub_date": metadata.get("pub_date"),
            "audio_gcs_uri": metadata.get("gcs_uri") or transcript_record.get("audio_gcs_uri"),
            "transcript_gcs_uri": f"gs://placeholder/{TRANSCRIPT_PREFIX}/{show_slug}/{episode_id}.json",
            "quality": quality,
            "bills": [],
            "people": [],
            "topics": [],
            "stances": [],
            "claims": [],
            "summary_fields": {
                "top_topics": [],
                "bill_refs": [],
                "people_mentioned": [],
                "people_normalized": [],
            },
            "enriched_at": datetime.now(timezone.utc).isoformat(),
            "engine": "rule_based_v1",
        }

    # Prefer content after ads when possible: keep full text but also scan description.
    search_text = f"{title}. {description}\n\n{transcript}"
    bills = extract_bills(search_text)
    topics = extract_topics(search_text)
    people = extract_people(transcript, title=title, description=description)
    stances = extract_stances(search_text, bills, topics)
    claims = extract_claims(transcript, topics)

    return {
        "episode_id": episode_id,
        "show_slug": show_slug,
        "title": title,
        "pub_date": metadata.get("pub_date"),
        "audio_gcs_uri": metadata.get("gcs_uri") or transcript_record.get("audio_gcs_uri"),
        "source_url": metadata.get("source_url"),
        "quality": quality,
        "bills": [
            {
                "ref": b.ref,
                "kind": b.kind,
                "normalized": b.normalized,
                "mentions": [asdict(m) for m in b.mentions],
            }
            for b in bills
        ],
        "people": [
            {
                "name": p.name,
                "normalized": p.normalized
                or re.sub(r"[^a-z0-9]+", "_", p.name.lower()).strip("_"),
                "role_hint": p.role_hint,
                "mention_count": p.mention_count,
                "mentions": [asdict(m) for m in p.mentions],
            }
            for p in people
        ],
        "topics": [
            {
                "topic": t.topic,
                "score": t.score,
                "mentions": [asdict(m) for m in t.mentions],
            }
            for t in topics
        ],
        "stances": [asdict(s) for s in stances],
        "claims": claims,
        "summary_fields": {
            "top_topics": [t.topic for t in topics[:5]],
            "bill_refs": [b.ref for b in bills],
            "people_mentioned": [p.name for p in people[:10]],
            "people_normalized": [
                (
                    p.normalized
                    or re.sub(r"[^a-z0-9]+", "_", p.name.lower()).strip("_")
                )
                for p in people[:10]
            ],
        },
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "engine": "rule_based_v1",
    }


def enrich_missing(
    limit: int | None = None,
    force: bool = False,
    show_slug_filter: str | None = None,
) -> dict[str, int]:
    config = load_config()
    bucket_name = config["bucket_name"]
    if not bucket_name:
        raise ValueError("Set GCP_BUCKET_NAME in .env")

    client = get_storage_client(config)
    bucket = client.bucket(bucket_name)
    stats = {
        "checked": 0,
        "enriched": 0,
        "skipped": 0,
        "unusable": 0,
        "errors": 0,
    }

    prefix = f"{TRANSCRIPT_PREFIX}/"
    if show_slug_filter:
        prefix = f"{TRANSCRIPT_PREFIX}/{show_slug_filter}/"

    for transcript_blob in bucket.list_blobs(prefix=prefix):
        if not transcript_blob.name.endswith(".json"):
            continue

        parsed = parse_transcript_path(transcript_blob.name)
        if not parsed:
            continue
        show_slug, episode_id = parsed
        stats["checked"] += 1

        out_path = enrichment_blob_path(show_slug, episode_id)
        out_blob = bucket.blob(out_path)
        if out_blob.exists() and not force:
            stats["skipped"] += 1
            continue

        # Limit applies to usable enrichments only so short/bad ASR
        # does not burn the budget before good Whisper transcripts.
        if limit is not None and stats["enriched"] >= limit:
            break

        try:
            transcript_record = json.loads(transcript_blob.download_as_text())
            meta_blob = bucket.blob(metadata_blob_path(show_slug, episode_id))
            metadata = json.loads(meta_blob.download_as_text()) if meta_blob.exists() else {}
            enrichment = enrich_episode(show_slug, episode_id, transcript_record, metadata)
            enrichment["transcript_gcs_uri"] = f"gs://{bucket_name}/{transcript_blob.name}"

            if not enrichment["quality"]["usable"]:
                stats["unusable"] += 1
                log.info(
                    "Marked unusable %s (%s)",
                    transcript_blob.name,
                    enrichment["quality"].get("reason"),
                )
            else:
                log.info(
                    "Enriched %s topics=%s bills=%s people=%s",
                    transcript_blob.name,
                    enrichment["summary_fields"]["top_topics"],
                    enrichment["summary_fields"]["bill_refs"],
                    enrichment["summary_fields"]["people_mentioned"][:5],
                )
                stats["enriched"] += 1

            out_blob.upload_from_string(
                json.dumps(enrichment, indent=2),
                content_type="application/json",
            )
        except Exception:
            log.exception("Failed enrichment for %s", transcript_blob.name)
            stats["errors"] += 1

    log.info(
        "Done. checked=%(checked)d enriched=%(enriched)d skipped=%(skipped)d "
        "unusable=%(unusable)d errors=%(errors)d",
        stats,
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enrich podcast transcripts into bills/topics/people/stance for querying",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max new enrichments this run")
    parser.add_argument("--force", action="store_true", help="Overwrite existing enrichment JSON")
    parser.add_argument("--show", type=str, default=None, help="Only enrich one show slug")
    args = parser.parse_args()
    enrich_missing(limit=args.limit, force=args.force, show_slug_filter=args.show)
    return 0


if __name__ == "__main__":
    sys.exit(main())
