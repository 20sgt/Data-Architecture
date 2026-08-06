# Deep-Dive Plan — Civic Podcast Search (M2)

## Chosen option

**Structured civic search over podcast silver tables**, with quote windows suitable for later RAG / MCP / chat.

Users (and tools) should answer questions like:

- “Which episodes discuss Prop C and homelessness?”
- “What did Scott Wiener say about housing on Fifth & Mission?”
- “Recent Voice of SF episodes mentioning elections or budget?”

without scrubbing raw MP3s or depending on exact ASR spelling.

## Why it fits the domain

| Domain need | How this pipeline fits |
|-------------|------------------------|
| Local politics literacy | Podcasts explain bills and officials in everyday language |
| Findability | Normalized keys (`prop_c`, `homelessness`, `scott_wiener`) |
| Trust | Quote windows + `transcript_gcs_uri` / episode titles for citations |
| Cost for a student team | Local Whisper + rule-based enrich; no STT / LLM API bill |

## What is already built (silver)

Tables (SQLite locally; JSONL in GCS):

- `episodes` — catalog + `usable` flag  
- `episode_bills` — `bill_normalized`, ref, quote  
- `episode_topics` — topic + score + quote  
- `episode_people` — `person_normalized`, role, quote  
- `episode_stances` / `episode_claims` — attitude + claim sentences  

CLI: `query_silver.py --bill | --topic | --person`.

## Evaluation plan (KPIs)

### Pipeline health

| KPI | Target / method |
|-----|-----------------|
| Transcript coverage | ≥ 95% of audio files have `transcripts_whisper` JSON |
| Usable rate | Share of episodes with `usable=1` after quality gate |
| Freshness lag | Hours from RSS publish → bronze → transcript → silver |
| Job success | Weekly Cloud ingest succeeds; local Whisper catch-up documented |

### Extraction quality

| KPI | Target / method |
|-----|-----------------|
| Bill precision (sample) | Manual audit of 50 random `episode_bills` rows; track false positives (e.g. ASR “Prop AND”) |
| Person precision (sample) | Audit known officials vs false-positive names on civic shows |
| Civic-show density | Avg topics/people per episode on `fifth-and-mission` + `fixing-our-city` |

### Query usefulness

| KPI | Target / method |
|-----|-----------------|
| Top-3 relevance | 10 fixed civic questions; useful episode in top 3 results |
| Citation rate | ≥ 90% of bill/topic/person rows include a non-empty quote |
| Latency | Local SQLite filter queries &lt; 2 seconds |

## Next increments (post-M2)

1. Tighten bill regex / blocklists to cut measure/prop false positives.  
2. Expand `representatives.json` from the team’s people scraper.  
3. Optional MCP / API: `search_podcasts(bill=, topic=, person=)`.  
4. Optional RAG over `claims` + transcript snippets with silver filters as recall gate.

## Success for this milestone

M2 is satisfied when:

1. Processing jobs exist and have been run (ingest, Whisper, enrich, silver).  
2. Silver tables support bill / topic / person queries.  
3. This plan documents option, domain fit, and evaluation KPIs (above).
