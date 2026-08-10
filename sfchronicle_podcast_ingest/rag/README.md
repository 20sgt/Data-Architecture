# Free podcast RAG (retrieve only)

Natural-language → **top 3 citation chunks** (quote, title, podcast URL), using
**word overlap** against silver SQLite + a **last-4-weeks gold matters** export.

No embeddings, no paid search APIs, no LLM call in this folder. Output JSON is
ready to hand to an LLM later for summarization.

```text
question + matters.json (gold export)
        → filter matters to last N days
        → tokenize query + matter names/files
        → score podcast silver chunks (quotes)
        → status=ok + 3 chunks  OR  status=no_recent_data
```

## Setup

Uses the existing package venv and `data/podcast_silver.sqlite` (run `./run_silver.sh` first).

Export gold matters from Databricks to JSON (see `data/matters.example.json`), then:

```bash
cp rag/data/matters.example.json rag/data/matters.json
# replace with a real export when ready
```

## Run

```bash
# from sfchronicle_podcast_ingest/
./.venv/bin/python3 -m rag.query "What are supervisors saying about zoning on Carroll Avenue?"
./.venv/bin/python3 -m rag.query "housing tax VoIP" --window-days 28
./.venv/bin/python3 -m rag.query "Prop C" --matters rag/data/matters.example.json
```

## Output shape

```json
{
  "query": "...",
  "window_days": 28,
  "as_of": "2026-08-10",
  "matters_considered": [{"matter_file": "250823", "matter_name": "..."}],
  "status": "ok",
  "chunks": [
    {"title": "...", "quote": "...", "url": "...", "score": 4, "episode_id": "...", "kind": "bill"}
  ]
}
```

`status` is `no_recent_data` when there are no gold matters in the window **or**
no podcast chunks with word overlap. Hand that JSON to an LLM with
`rag/prompts/summarize.txt`.

## Files

| File | Role |
|------|------|
| `tokenize.py` | Cheap word tokens + stopwords |
| `matters.py` | Load/filter gold matter export |
| `chunks.py` | Build quote chunks from silver SQLite |
| `retrieve.py` | Score + top-k / empty status |
| `query.py` | CLI |
| `data/matters.example.json` | Sample gold rows |
| `prompts/summarize.txt` | Later LLM prompt template |
