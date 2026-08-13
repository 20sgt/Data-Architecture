#!/usr/bin/env bash
# Cloud weekly: ingest → budget-capped Whisper → enrich → silver (GCS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "===== Cloud pipeline $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
echo "WHISPER_MODEL=${WHISPER_MODEL:-tiny} MAX_EPISODES=${WHISPER_MAX_EPISODES:-5}"

python ingest.py
python transcribe.py
python enrich.py
python silver.py --gcs-only

echo "===== Done $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
