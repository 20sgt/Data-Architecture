#!/usr/bin/env bash
# Local: ingest → Whisper → enrich → silver (SQLite + GCS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"
cd "${SCRIPT_DIR}"

PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON}" ]]; then
  echo "Missing ${PYTHON} — run: python3 -m venv .venv && .venv/bin/python3 -m pip install -r requirements.txt" >&2
  exit 1
fi

{
  echo "===== Local pipeline $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
  "${PYTHON}" ingest.py
  "${PYTHON}" transcribe.py
  "${PYTHON}" enrich.py
  "${PYTHON}" silver.py
  echo "===== Done $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
} 2>&1 | tee -a "${LOG_DIR}/local_pipeline.log"
