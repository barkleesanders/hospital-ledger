#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${REMOTE:-mac-mini}"
REMOTE_DIR="${REMOTE_DIR:-/Users/barkleesanders/projects/hospital-ledger}"
SESSION="${SESSION:-hospital-ledger-openclaw}"
RESOURCE_PERCENT="${RESOURCE_PERCENT:-70}"
CHUNK_SIZE="${CHUNK_SIZE:-50}"
PARSER_GB_PER_WORKER="${PARSER_GB_PER_WORKER:-5}"
MAX_TOTAL_WORKERS="${MAX_TOTAL_WORKERS:-8}"
MIN_FREE_GB="${MIN_FREE_GB:-10}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none)
RSYNC_SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none"

cd "$ROOT"

ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '$REMOTE_DIR'"

rsync -az --partial --delete --progress -e "$RSYNC_SSH" \
  --exclude '.venv/' \
  --exclude '.wrangler/' \
  --exclude 'data/parsed/' \
  --exclude 'data/raw/' \
  --exclude 'data/*.status.json' \
  --exclude 'data/*_status.json' \
  --exclude 'data/cms_validation/' \
  --exclude 'data/_pages_bundle/' \
  --exclude 'data/_pages_bundle_test/' \
  --exclude 'site/data/prices/' \
  --exclude 'site/data/cpt-index.json' \
  --exclude '*.log' \
  ./ "$REMOTE:$REMOTE_DIR/"

ssh "${SSH_OPTS[@]}" "$REMOTE" "
  set -euo pipefail
  export PATH=/opt/homebrew/bin:/usr/local/bin:\$PATH
  cd '$REMOTE_DIR'
  test -d .venv || python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  if test -f requirements-fast.txt; then
    .venv/bin/python -m pip install -r requirements-fast.txt || echo 'optional fast parser dependencies failed to install; continuing with stdlib parser'
  fi
  if tmux has-session -t '$SESSION' 2>/dev/null; then
    echo 'tmux session $SESSION already exists'
    exit 0
  fi
  tmux new-session -d -s '$SESSION' \"cd '$REMOTE_DIR' && source scripts/cloudflare_env.zsh 2>/dev/null || true; RESOURCE_PERCENT='$RESOURCE_PERCENT' CHUNK_SIZE='$CHUNK_SIZE' PARSER_GB_PER_WORKER='$PARSER_GB_PER_WORKER' MAX_TOTAL_WORKERS='$MAX_TOTAL_WORKERS' MIN_FREE_GB='$MIN_FREE_GB' .venv/bin/python scripts/openclaw_macmini_pipeline.py\"
  tmux has-session -t '$SESSION'
  echo 'launched $SESSION on $REMOTE'
"
