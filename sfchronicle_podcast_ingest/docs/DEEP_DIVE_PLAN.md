# Deep-dive plan (M2)

## Choice

**Structured civic search** over podcast silver tables (bills / people / topics + quotes).
Optional later: RAG on the same transcripts.

Example questions: “episodes about Prop C and homelessness,” “Scott Wiener on housing.”

## Why it fits

Podcasts explain local politics in plain language. Normalized keys (`prop_c`, `scott_wiener`)
ignore ASR spelling. Quotes support citations. Cost stays low (Whisper + rules, no STT/LLM APIs).

## Already built

SQLite / GCS JSONL: `episodes`, `episode_bills`, `episode_topics`, `episode_people`,
`episode_stances`, `episode_claims`. Query with `query_silver.py`.

Free retrieve path (`rag/`): NL question + last-4-weeks gold matters JSON → top 3
chunks `{quote, title, url}` via word overlap, or `no_recent_data` for a later LLM.
No embeddings or paid APIs.

## How we’ll evaluate

| KPI | Method |
|-----|--------|
| Coverage | % episodes with usable Whisper transcript |
| Precision | Spot-check ~50 bill/person extractions |
| Usefulness | 10 test questions; useful hit in top 3 |
| Latency | SQLite filters under ~2s |

## Done for M2 when

Jobs have run, silver answers bill/topic/person queries, and this plan is on the slides.
