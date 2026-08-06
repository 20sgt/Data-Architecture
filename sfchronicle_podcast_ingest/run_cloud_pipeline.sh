#!/usr/bin/env bash
set -euo pipefail

# Cloud weekly job (scheduled only — Sunday 03:00 PT via Cloud Scheduler):
#   1) ingest new episodes into GCS
#   2) Whisper-transcribe missing audio with hard spend guards
#   3) enrich missing enrichment JSON
#   4) rebuild silver JSONL in GCS
#
# No Google Speech-to-Text. Whisper = faster-whisper tiny on CPU.
# Spend caps (defaults): max 5 episodes, 20 minutes, ~$0.25 est. Cloud Run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "===== Cloud podcast pipeline started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
echo "Using Python: $(command -v python)"
echo "WHISPER_MODEL=${WHISPER_MODEL:-tiny} device=${WHISPER_DEVICE:-cpu}"
echo "Guards: MAX_EPISODES=${WHISPER_MAX_EPISODES:-5} MAX_RUNTIME_MIN=${WHISPER_MAX_RUNTIME_MINUTES:-20} BUDGET_USD=${WHISPER_BUDGET_USD:-0.25}"

echo "--- ingest ---"
python ingest.py

echo "--- transcribe (budget-limited Whisper; \$0 Speech-to-Text) ---"
python transcribe.py

echo "--- enrich ---"
python enrich.py

echo "--- silver (GCS JSONL) ---"
python silver.py --gcs-only

echo "===== Cloud podcast pipeline finished: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
