from enrich import (
    assess_quality,
    enrich_episode,
    extract_bills,
    extract_claims,
    extract_people,
    extract_stances,
    extract_topics,
)


SAMPLE_TEXT = """
I'm Cecilia Lei and this is Fifth and Mission. Affluent Marin is facing its own
homelessness crisis. Binford Road is home to dozens of unhoused people and the
population there has ballooned in recent years. Chronicle reporter Annie Vainshtein
joins host Cecilia Lei to talk about Prop C and Assembly Bill 1487. Supervisor
Scott Wiener said he supports the housing measure, while critics oppose the ordinance
because they think it is over the top. The county faces a severe affordability crisis
and is struggling to contain encampments. Local officials say shelter capacity is
limited and mental health services are stretched thin across the region. Residents
near the encampment say theft and public safety concerns have risen, while advocates
argue that permanent housing and rental assistance are the only durable solutions.
Transit options are limited for people living outdoors, and hospitals report more
overdose cases tied to fentanyl. The Board of Supervisors is expected to debate a
related budget item next week as city leaders weigh funding for new shelter beds.
Annie Vainshtein reports that many people living on Binford Road have been there for
months after losing work during the pandemic. Scott Wiener told the Chronicle he
supports Prop C because it could unlock more affordable housing, but some neighbors
oppose the plan and worry about density near schools. Cecilia Lei asks whether the
city can balance compassion with enforcement without criminalizing poverty itself.
"""


def test_extract_bills_finds_prop_and_assembly_bill():
    bills = extract_bills(SAMPLE_TEXT)
    refs = {b.ref for b in bills}
    assert "Prop C" in refs
    assert "AB 1487" in refs


def test_extract_bills_ignores_proper_not_prop_er():
    bills = extract_bills("Schools can operate safely with proper precautions and outdoor dining.")
    assert bills == []


def test_extract_topics_ranks_homelessness_and_housing():
    topics = extract_topics(SAMPLE_TEXT)
    topic_names = [t.topic for t in topics]
    assert "homelessness" in topic_names
    assert "housing" in topic_names
    assert topics[0].score >= topics[-1].score


def test_extract_people_finds_known_names():
    people = extract_people(SAMPLE_TEXT, title="Inside Marin Homeless Encampment")
    names = {p.name.lower() for p in people}
    assert "cecilia lei" in names
    assert "scott wiener" in names


def test_extract_stances_detects_support_and_oppose():
    bills = extract_bills(SAMPLE_TEXT)
    topics = extract_topics(SAMPLE_TEXT)
    stances = extract_stances(SAMPLE_TEXT, bills, topics)
    stance_map = {(s.target_type, s.target.lower(), s.stance) for s in stances}
    assert any(s == "supports" or s == "opposes" or s == "concerned" for *_, s in stance_map)


def test_extract_claims_returns_topic_sentences():
    topics = extract_topics(SAMPLE_TEXT)
    claims = extract_claims(SAMPLE_TEXT, topics)
    assert claims
    assert all("about" in claim and claim["text"] for claim in claims)


def test_assess_quality_rejects_garbage_asr():
    garbage = " ".join(["Here. You. Oh yeah."] * 80)
    quality = assess_quality(garbage)
    assert quality["usable"] is False


def test_enrich_episode_summary_fields():
    record = enrich_episode(
        show_slug="fifth-and-mission",
        episode_id="abc123",
        transcript_record={"transcript": SAMPLE_TEXT, "audio_gcs_uri": "gs://bucket/a.mp3"},
        metadata={
            "title": "Inside Marin County Homeless Encampment",
            "description": "Discussion of homelessness funding and Prop C.",
            "pub_date": "2023-01-01",
            "gcs_uri": "gs://bucket/a.mp3",
            "source_url": "https://example.com/audio.mp3",
        },
    )
    assert record["quality"]["usable"] is True
    assert "homelessness" in record["summary_fields"]["top_topics"]
    assert record["summary_fields"]["bill_refs"]
    assert record["engine"] == "rule_based_v1"
