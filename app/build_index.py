#!/usr/bin/env python3
"""Build the podcast transcript search index used by the NL query app.

Pulls Whisper transcripts + the episode silver table out of GCS and loads them
into a local SQLite FTS5 index. FTS5 ships with Python's sqlite3, gives us BM25
ranking for free, and needs no embedding model or vector store.

Transcripts are indexed as ~60-second chunks rather than whole episodes: a
high-scoring chunk means a *sustained* discussion of the topic (not a passing
mention), and the chunk's start time is what lets the app say "listen at 14:32".

Run:  python app/build_index.py
"""
import json
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TRANSCRIPTS = os.path.join(DATA, "transcripts")
EPISODES_JSONL = os.path.join(DATA, "episodes.jsonl")
DB = os.path.join(DATA, "podcast_search.sqlite")

PROJECT = os.getenv("GCP_PROJECT_ID", "corn-off-the-cobb")
BUCKET = os.getenv("GCP_BUCKET_NAME", "podcasts-audio-files")
CHUNK_SECONDS = 60


def fetch():
    """Mirror the transcripts + episode table locally (incremental; safe to re-run)."""
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    subprocess.run(
        ["gcloud", "storage", "rsync", "-r", "--project", PROJECT,
         f"gs://{BUCKET}/podcasts/transcripts_whisper", TRANSCRIPTS],
        check=True,
    )
    subprocess.run(
        ["gcloud", "storage", "cp", "--project", PROJECT,
         f"gs://{BUCKET}/podcasts/silver/episodes.jsonl", EPISODES_JSONL],
        check=True,
    )


def chunk(segments, window=CHUNK_SECONDS):
    """Group Whisper segments into ~`window`-second chunks of (start_s, text)."""
    out, buf, start = [], [], None
    for seg in segments:
        text = (seg.get("transcript") or "").strip()
        if not text:
            continue
        if start is None:
            start = seg.get("start") or 0.0
        buf.append(text)
        if (seg.get("end") or start) - start >= window:
            out.append((start, " ".join(buf)))
            buf, start = [], None
    if buf:
        out.append((start, " ".join(buf)))
    return out


def build():
    episodes = {}
    with open(EPISODES_JSONL) as fh:
        for line in fh:
            row = json.loads(line)
            episodes[row["episode_id"]] = row

    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    conn.executescript("""
        CREATE TABLE episodes (
            episode_id TEXT PRIMARY KEY,
            show_slug  TEXT,
            title      TEXT,
            pub_date   TEXT,
            source_url TEXT
        );
        CREATE VIRTUAL TABLE chunks USING fts5(
            text,
            episode_id UNINDEXED,
            start_s    UNINDEXED
        );
    """)

    n_ep = n_chunk = 0
    for show in sorted(os.listdir(TRANSCRIPTS)):
        show_dir = os.path.join(TRANSCRIPTS, show)
        if not os.path.isdir(show_dir):
            continue
        for fname in sorted(os.listdir(show_dir)):
            if not fname.endswith(".json"):
                continue
            episode_id = fname[:-5]
            meta = episodes.get(episode_id)
            # Skip episodes the enrichment step flagged as bad ASR / music-only,
            # and any transcript with no row in the silver table.
            if not meta or not meta.get("usable"):
                continue
            with open(os.path.join(show_dir, fname)) as fh:
                doc = json.load(fh)
            rows = chunk(doc.get("results") or [])
            if not rows:
                continue
            conn.execute(
                "INSERT INTO episodes VALUES (?,?,?,?,?)",
                (episode_id, meta.get("show_slug") or show, meta.get("title"),
                 meta.get("pub_date"), meta.get("source_url")),
            )
            conn.executemany(
                "INSERT INTO chunks (text, episode_id, start_s) VALUES (?,?,?)",
                [(text, episode_id, start) for start, text in rows],
            )
            n_ep += 1
            n_chunk += len(rows)

    conn.commit()
    conn.close()
    print(f"indexed {n_ep} episodes / {n_chunk} chunks -> {DB}")


def demo():
    segs = [
        {"transcript": "a", "start": 0.0, "end": 30.0},
        {"transcript": "b", "start": 30.0, "end": 70.0},   # crosses the 60s window
        {"transcript": "", "start": 70.0, "end": 71.0},    # empty -> dropped
        {"transcript": "c", "start": 71.0, "end": 80.0},   # tail, flushed at end
    ]
    assert chunk(segs) == [(0.0, "a b"), (71.0, "c")], chunk(segs)
    assert chunk([]) == []
    assert chunk([{"transcript": "  ", "start": 0.0, "end": 1.0}]) == []
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        if "--no-fetch" not in sys.argv:
            fetch()
        build()
