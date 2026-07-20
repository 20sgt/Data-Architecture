from enrich import parse_transcript_path
from silver import flatten_enrichment, write_sqlite


SAMPLE_ENRICHMENT = {
    "episode_id": "abc123",
    "show_slug": "fifth-and-mission",
    "title": "Inside Marin Homeless Encampment",
    "pub_date": "2023-01-01",
    "audio_gcs_uri": "gs://bucket/a.mp3",
    "transcript_gcs_uri": "gs://bucket/t.json",
    "source_url": "https://example.com/a.mp3",
    "quality": {"usable": True, "char_count": 2000, "reason": None},
    "bills": [
        {
            "ref": "Prop C",
            "kind": "proposition",
            "normalized": "prop_c",
            "mentions": [{"quote": "talk about Prop C", "start_char": 0, "end_char": 10}],
        }
    ],
    "people": [
        {
            "name": "Scott Wiener",
            "normalized": "scott_wiener",
            "role_hint": "state_senator",
            "mention_count": 2,
            "mentions": [{"quote": "Scott Wiener said", "start_char": 0, "end_char": 12}],
        }
    ],
    "topics": [
        {
            "topic": "homelessness",
            "score": 5,
            "mentions": [{"quote": "homelessness crisis", "start_char": 0, "end_char": 10}],
        }
    ],
    "stances": [
        {
            "target_type": "bill",
            "target": "Prop C",
            "stance": "supports",
            "confidence": 0.7,
            "quote": "supports Prop C",
        }
    ],
    "claims": [{"text": "The county faces a severe affordability crisis.", "about": ["housing"]}],
    "summary_fields": {
        "top_topics": ["homelessness"],
        "bill_refs": ["Prop C"],
        "people_mentioned": ["Scott Wiener"],
        "people_normalized": ["scott_wiener"],
    },
    "enriched_at": "2026-01-01T00:00:00+00:00",
    "engine": "rule_based_v1",
}


def test_parse_transcript_path_accepts_whisper_prefix():
    assert parse_transcript_path(
        "podcasts/transcripts_whisper/fifth-and-mission/abc123.json"
    ) == ("fifth-and-mission", "abc123")


def test_parse_transcript_path_accepts_legacy_prefix():
    assert parse_transcript_path(
        "podcasts/transcripts/fifth-and-mission/abc123.json"
    ) == ("fifth-and-mission", "abc123")


def test_parse_transcript_path_rejects_other_prefixes():
    assert parse_transcript_path("podcasts/enrichment/fifth-and-mission/abc123.json") is None
    assert parse_transcript_path("podcasts/audio/fifth-and-mission/abc123.mp3") is None


def test_flatten_enrichment_makes_query_rows():
    tables = flatten_enrichment(SAMPLE_ENRICHMENT)
    assert tables["episodes"][0]["episode_id"] == "abc123"
    assert tables["episodes"][0]["usable"] == 1
    assert tables["episode_bills"][0]["bill_normalized"] == "prop_c"
    assert tables["episode_topics"][0]["topic"] == "homelessness"
    assert tables["episode_people"][0]["person_normalized"] == "scott_wiener"
    assert tables["episode_stances"][0]["stance"] == "supports"
    assert tables["episode_claims"][0]["claim_text"]


def test_write_sqlite_roundtrip(tmp_path):
    import sqlite3

    from silver import empty_tables, merge_tables

    tables = empty_tables()
    merge_tables(tables, flatten_enrichment(SAMPLE_ENRICHMENT))
    db = tmp_path / "silver.sqlite"
    write_sqlite(tables, str(db))

    conn = sqlite3.connect(db)
    try:
        bills = conn.execute(
            "SELECT bill_normalized FROM episode_bills WHERE episode_id=?",
            ("abc123",),
        ).fetchall()
        assert bills == [("prop_c",)]
        people = conn.execute(
            "SELECT person_normalized FROM episode_people WHERE episode_id=?",
            ("abc123",),
        ).fetchall()
        assert people == [("scott_wiener",)]
    finally:
        conn.close()
