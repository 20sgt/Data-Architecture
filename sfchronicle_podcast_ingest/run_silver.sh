#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

PYTHON="${SCRIPT_DIR}/.venv/bin/python3"
if [[ ! -x "${PYTHON}" ]]; then
  echo "Missing venv Python at ${PYTHON}" >&2
  exit 1
fi

echo "Using Python: ${PYTHON}"
exec "${PYTHON}" "${SCRIPT_DIR}/silver.py" "$@"
