#!/usr/bin/env bash
set -euo pipefail

# Cloud weekly job (no Speech-to-Text / no paid LLM):
#   1) ingest new episodes into GCS
#   2) enrich any Whisper transcripts that already exist
#   3) rebuild silver JSONL tables in GCS
#
# Transcription stays local (Whisper). If no new transcripts exist yet,
# enrich/silver simply skip or rebuild from what is already there.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "===== Cloud podcast pipeline started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
echo "Using Python: $(command -v python)"

echo "--- ingest ---"
python ingest.py

echo "--- enrich (only transcripts already in transcripts_whisper/) ---"
python enrich.py

echo "--- silver (GCS JSONL only; SQLite is local) ---"
python silver.py --gcs-only

echo "Skipping cloud Whisper (run locally via ./run_transcribe.sh — \$0 STT)."
echo "===== Cloud podcast pipeline finished: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
