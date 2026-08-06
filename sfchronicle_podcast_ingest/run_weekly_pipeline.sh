#!/usr/bin/env bash
# Alias for local weekly cron / launchd (same as run_local_pipeline.sh).
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_local_pipeline.sh" "$@"
