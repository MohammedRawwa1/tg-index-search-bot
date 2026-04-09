#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cat <<'EOF'
Virtualenv created and dependencies installed.
Activate with:
  source .venv/bin/activate
To create a user session run:
  source .venv/bin/activate && python scripts/create_user_session.py
To run backfill after creating session:
  source .venv/bin/activate && python scripts/backfill.py
EOF