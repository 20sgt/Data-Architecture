#!/usr/bin/env bash
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
  echo "===== Weekly podcast pipeline started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
  echo "Using Python: ${PYTHON}"
  "${PYTHON}" ingest.py
  "${PYTHON}" transcribe.py
  echo "===== Weekly podcast pipeline finished: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
} 2>&1 | tee -a "${LOG_DIR}/weekly_pipeline.log"
