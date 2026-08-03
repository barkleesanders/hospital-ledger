#!/usr/bin/env bash
# Portable weekly runner. State lives in R2, compute may be replaced at any time.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${R2_ACCOUNT_ID:?missing R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?missing R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?missing R2_SECRET_ACCESS_KEY}"
: "${CLOUDFLARE_API_TOKEN:?missing CLOUDFLARE_API_TOKEN}"

export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
REMOTE=":s3,provider=Cloudflare,endpoint=${R2_ENDPOINT}:hl-mrf-parsed"

mkdir -p data/parsed data/refresh_logs db
# The state prefix is private operational state, separate from live API objects.
rclone copy "$REMOTE/_pipeline/state" data --include '/cloud_refresh_state.json' --include '/r2_upload_manifest.json' || true
rclone copy "$REMOTE/_pipeline/db" db --include '/hospital_ledger.db' || true
rclone sync "$REMOTE/parsed" data/parsed --fast-list --transfers 16 --checkers 32 || true

python3 scripts/build_db.py
python3 scripts/probe_mrf_urls.py --all --concurrency "${PROBE_CONCURRENCY:-32}"
python3 scripts/prepare_incremental_refresh.py --workers "${PROBE_CONCURRENCY:-32}"

# refresh.sh owns audits, safe R2 publication, the allowlisted data commit, deploy,
# and post-deploy verification. Linux has no taskpolicy, so its nice fallback applies.
WORKERS="${INGEST_WORKERS:-$(python3 scripts/tune_ingest_workers.py 2>/dev/null || echo 4)}"
python3 scripts/batch_ingest.py --resume --all --workers "$WORKERS" --item-timeout-seconds 1800
bash scripts/refresh.sh --skip-ingest

# Advance validators only after publication and the live audit both succeed.
python3 scripts/prepare_incremental_refresh.py --workers "${PROBE_CONCURRENCY:-32}" --commit
rclone copy data/cloud_refresh_state.json "$REMOTE/_pipeline/state/"
rclone copy data/r2_upload_manifest.json "$REMOTE/_pipeline/state/" || true
rclone copy db/hospital_ledger.db "$REMOTE/_pipeline/db/"
