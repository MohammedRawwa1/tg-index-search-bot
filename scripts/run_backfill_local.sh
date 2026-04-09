#!/usr/bin/env bash
set -euo pipefail
# usage: ./run_backfill_local.sh <TARGET_CHAT_ID>
if [ "$#" -ge 1 ]; then
  export TARGET_CHAT_ID="$1"
fi
. .venv/bin/activate
python scripts/backfill.py
