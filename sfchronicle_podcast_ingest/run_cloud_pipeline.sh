#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "===== Cloud podcast pipeline started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
echo "Using Python: $(command -v python)"
python ingest.py
python transcribe.py
echo "===== Cloud podcast pipeline finished: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
