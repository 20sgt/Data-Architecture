#!/usr/bin/env bash
# Local full pipeline (free inference): ingest → Whisper → enrich → silver.
# Safe to re-run: each step skips work that is already done.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

cd "${SCRIPT_DIR}"

PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON}" ]]; then
  echo "Missing venv Python at ${PYTHON}" >&2
  echo "Create it with: python3 -m venv .venv && .venv/bin/python3 -m pip install -r requirements.txt" >&2
  exit 1
fi

{
  echo "===== Local podcast pipeline started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
  echo "Using Python: ${PYTHON}"
  echo "--- ingest ---"
  "${PYTHON}" ingest.py
  echo "--- transcribe (local Whisper, \$0 STT) ---"
  "${PYTHON}" transcribe.py
  echo "--- enrich (rule-based, \$0) ---"
  "${PYTHON}" enrich.py
  echo "--- silver (SQLite + GCS JSONL, \$0 APIs) ---"
  "${PYTHON}" silver.py
  echo "===== Local podcast pipeline finished: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
} 2>&1 | tee -a "${LOG_DIR}/local_pipeline.log"
