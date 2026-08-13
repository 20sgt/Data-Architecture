"""Tests for free keyword RAG (no network, no paid APIs)."""

from datetime import date

from rag.chunks import Chunk
from rag.matters import Matter, filter_recent_matters, load_matters
from rag.retrieve import retrieve, score_chunk
from rag.tokenize import tokenize


def test_tokenize_drops_stopwords_keeps_file_numbers():
    tokens = tokenize("The ordinance for file 250823 on Carroll Avenue")
    assert "250823" in tokens
    assert "carroll" in tokens
    assert "the" not in tokens
    assert "for" not in tokens


def test_filter_recent_matters_uses_any_date_in_window():
    matters = [
        Matter(
            matter_file="250823",
            matter_name="Carroll Avenue",
            dates=(date(2026, 1, 23),),
        ),
        Matter(
            matter_file="999999",
            matter_name="Old matter",
            dates=(date(2020, 1, 1),),
        ),
        Matter(matter_file="000000", matter_name="No dates", dates=()),
    ]
    recent = filter_recent_matters(
        matters, window_days=28, as_of=date(2026, 2, 10)
    )
    assert [m.matter_file for m in recent] == ["250823"]


def test_load_matters_example_file():
    matters = load_matters("rag/data/matters.example.json")
    assert len(matters) >= 2
    assert matters[0].matter_file == "250823"


def test_retrieve_ok_returns_three_or_fewer_distinct_episodes():
    matters = [
        Matter(
            matter_file="250823",
            matter_name="Planning Code Zoning Map 1236 Carroll Avenue",
            aliases=("Carroll Avenue",),
            dates=(date(2026, 1, 23),),
        )
    ]
    chunks = [
        Chunk(
            episode_id="ep1",
            title="Zoning fight on Carroll Avenue",
            url="https://example.com/1",
            quote="Supervisors debated the zoning map change near Carroll Avenue parcels.",
            kind="topic",
            label="housing",
        ),
        Chunk(
            episode_id="ep1",
            title="Zoning fight on Carroll Avenue",
            url="https://example.com/1",
            quote="Another quote about Carroll Avenue from the same episode.",
            kind="bill",
            label="File 250823",
        ),
        Chunk(
            episode_id="ep2",
            title="Planning Code updates",
            url="https://example.com/2",
            quote="The planning code ordinance would rezone industrial land to public use.",
            kind="claim",
        ),
        Chunk(
            episode_id="ep3",
            title="Unrelated sports",
            url="https://example.com/3",
            quote="The Giants won in extra innings after a wild pitch.",
            kind="claim",
        ),
    ]
    result = retrieve(
        "Tell me about Carroll Avenue zoning",
        as_of=date(2026, 2, 10),
        window_days=28,
        matters=matters,
        chunks=chunks,
        top_k=3,
    )
    assert result["status"] == "ok"
    assert 1 <= len(result["chunks"]) <= 3
    assert len({c["episode_id"] for c in result["chunks"]}) == len(result["chunks"])
    assert all(c["url"] and c["quote"] and c["title"] for c in result["chunks"])


def test_retrieve_no_recent_matters():
    result = retrieve(
        "zoning",
        as_of=date(2026, 8, 1),
        window_days=28,
        matters=[
            Matter(
                matter_file="250823",
                matter_name="Carroll Avenue",
                dates=(date(2026, 1, 23),),
            )
        ],
        chunks=[],
    )
    assert result["status"] == "no_recent_data"
    assert result["chunks"] == []


def test_retrieve_matters_but_no_podcast_overlap():
    result = retrieve(
        "completely unrelated query xyzzy",
        as_of=date(2026, 2, 10),
        window_days=28,
        matters=[
            Matter(
                matter_file="250823",
                matter_name="Carroll Avenue zoning",
                dates=(date(2026, 1, 23),),
            )
        ],
        chunks=[
            Chunk(
                episode_id="ep9",
                title="Baseball night",
                url="https://example.com/9",
                quote="The pitcher threw a perfect game at the ballpark downtown.",
                kind="claim",
            )
        ],
    )
    # Matter tokens (carroll, zoning, ...) may still match nothing in baseball chunk.
    # Query tokens won't either → no_recent_data.
    assert result["status"] == "no_recent_data"


def test_score_chunk_counts_overlap():
    chunk = Chunk(
        episode_id="e",
        title="VoIP tax",
        url="https://example.com/v",
        quote="Access line tax rules for VoIP telephone numbers.",
        kind="bill",
        label="251002",
    )
    score = score_chunk(
        chunk,
        tokenize("voip access tax 251002"),
        tokenize("251002 voip"),
    )
    assert score >= 3.0


def test_score_chunk_requires_query_overlap_when_query_present():
    chunk = Chunk(
        episode_id="e",
        title="Healthcare staffing",
        url="https://example.com/h",
        quote="Department of Public Health street medicine teams.",
        kind="topic",
        label="healthcare",
    )
    # Matter tokens alone must not rank an unrelated chunk when query has words.
    score = score_chunk(
        chunk,
        tokenize("carroll avenue zoning"),
        tokenize("department public health carroll"),
    )
    assert score == 0.0
