#!/usr/bin/env bash
# Safe weekly refresh for ephemeral Linux workers. Durable state lives in R2.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prefer HTTP proxy variables. Python clients can misinterpret a SOCKS proxy
# when their optional SOCKS dependency is not installed.
unset ALL_PROXY all_proxy
export AWS_EC2_METADATA_DISABLED=true
export PATH="$ROOT/node_modules/.bin:$PATH"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$ROOT/.runtime/xdg}"
mkdir -p "$XDG_CONFIG_HOME" data/parsed data/refresh_logs data/refresh_runs db public/data/prices

PYTHON="${HOSPITAL_LEDGER_PYTHON:-$ROOT/.venv/bin/python3}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi

BUCKET="${PARSED_R2_BUCKET:-hl-mrf-parsed}"
R2_WORKERS="${R2_WORKERS:-16}"
PROBE_WORKERS="${PROBE_CONCURRENCY:-32}"
INGEST_WORKERS="${INGEST_WORKERS:-4}"
MAX_REGRESSION_PCT="${MAX_REGRESSION_PCT:-5}"
RUN_ID="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
RUN_DIR="$ROOT/data/refresh_runs/$RUN_ID"
BACKUP_DIR="$RUN_DIR/hospital-backup"
BASELINE_DIR="$RUN_DIR/baseline-data"
RUN_LOG="$ROOT/data/refresh_logs/cloud-$RUN_ID.log"
RUN_STATUS_FILE="$RUN_DIR/run-status.json"
ROLLBACK_MANIFEST="$RUN_DIR/r2-rollback.json"
PUBLICATION_KEYS="$RUN_DIR/publication-keys.txt"
REMOTE_AGGREGATE_KEYS="$RUN_DIR/remote-aggregate-keys.txt"
ROLLBACK_KEYS="$RUN_DIR/rollback-keys.txt"
STALE_KEYS="$RUN_DIR/stale-keys.txt"
DEPLOYMENTS_JSON="$RUN_DIR/deployments-before.json"
mkdir -p "$RUN_DIR"

PLAN_ONLY=0
NO_DEPLOY=0
FORCE_ALL=0
for arg in "$@"; do
  case "$arg" in
    --plan-only) PLAN_ONLY=1 ;;
    --no-deploy) NO_DEPLOY=1 ;;
    --force-all) FORCE_ALL=1 ;;
    -h|--help)
      echo "usage: scripts/cloud_refresh.sh [--plan-only] [--no-deploy] [--force-all]"
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

R2_ROLLBACK_ARMED=0
WORKER_ROLLBACK_ARMED=0
PREVIOUS_WORKER_VERSION=""
ROLLBACK_FAILURES=0
PRODUCTION_STARTED=0
BOOTSTRAP=0

write_run_status() {
  local status="$1" detail="${2:-}"
  "$PYTHON" - "$RUN_ID" "$status" "$detail" "$RUN_STATUS_FILE" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

run_id, status, detail, output = sys.argv[1:]
try:
    plan = json.loads(Path("data/cloud_refresh_plan.json").read_text())
except (OSError, json.JSONDecodeError):
    plan = {}
payload = {
    "run_id": run_id,
    "status": status,
    "detail": detail,
    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "plan": {
        "candidates": plan.get("candidate_count", plan.get("probed", 0)),
        "probed": plan.get("probed", 0),
        "reachable": plan.get("reachable", 0),
        "unavailable": plan.get("unavailable", 0),
        "changed": len(plan.get("changed", [])) if isinstance(plan.get("changed"), list) else 0,
    },
}
path = Path(output)
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
temporary.replace(path)
PY
}

upload_run_status() {
  "$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
    "$BUCKET" "_pipeline/runs/$RUN_ID.json" "$RUN_STATUS_FILE"
  "$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
    "$BUCKET" "_pipeline/runs/latest.json" "$RUN_STATUS_FILE"
}

notify_failure() {
  local code="$1"
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    return 0
  fi
  local tail_log="(no log)"
  if [ -f "$RUN_LOG" ]; then
    tail_log="$(tail -n 20 "$RUN_LOG" 2>/dev/null | tail -c 1400)"
  fi
  curl -sS -m 20 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=Hospital Ledger cloud refresh failed
run: $RUN_ID
exit: $code
rollback_failures: $ROLLBACK_FAILURES
$tail_log" >/dev/null 2>&1 || true
}

on_exit() {
  local code=$?
  trap - EXIT
  set +e
  if [ "$code" -ne 0 ]; then
    if [ "$WORKER_ROLLBACK_ARMED" = 1 ] && [ -n "$PREVIOUS_WORKER_VERSION" ]; then
      echo "Rolling Worker back to version $PREVIOUS_WORKER_VERSION" >&2
      wrangler rollback "$PREVIOUS_WORKER_VERSION" --yes \
        --message "Automatic rollback after failed Hospital Ledger refresh $RUN_ID" || ROLLBACK_FAILURES=$((ROLLBACK_FAILURES + 1))
    fi
    if [ "$R2_ROLLBACK_ARMED" = 1 ] && [ -s "$ROLLBACK_MANIFEST" ]; then
      echo "Restoring live R2 objects from the pre-publication snapshot" >&2
      "$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" \
        restore-snapshot "$ROLLBACK_MANIFEST" || ROLLBACK_FAILURES=$((ROLLBACK_FAILURES + 1))
    fi
    if [ "$PRODUCTION_STARTED" = 1 ]; then
      write_run_status "failed" "exit $code; rollback_failures=$ROLLBACK_FAILURES"
      upload_run_status || true
    fi
    notify_failure "$code"
  fi
  find "$ROOT/data" -maxdepth 1 \
    \( -name 'cpt_stream_*.tsv*' -o -name 'cpt_detail_*.tsv*' \) -delete 2>/dev/null || true
  exit "$code"
}
trap on_exit EXIT

exec > >(tee -a "$RUN_LOG") 2>&1

run_planner() {
  local args=(--workers "$PROBE_WORKERS")
  if [ "$FORCE_ALL" = 1 ]; then
    args+=(--force-all)
  fi
  "$PYTHON" scripts/prepare_incremental_refresh.py "${args[@]}"
}

require_runtime() {
  "$PYTHON" - <<'PY'
missing = []
for name in ("boto3", "httpx", "ijson", "openpyxl"):
    try:
        __import__(name)
    except ImportError:
        missing.append(name)
if missing:
    raise SystemExit("missing Python runtime packages: " + ", ".join(missing))
PY
  test -x node_modules/.bin/wrangler
  test -f src/index.tsx
  test -f public/.assetsignore
  local free_gb
  free_gb="$(df -Pk "$ROOT" | awk 'NR==2 {print int($4/1024/1024)}')"
  if [ "$free_gb" -lt "${MIN_START_FREE_GB:-35}" ]; then
    echo "insufficient free disk: ${free_gb} GiB" >&2
    return 1
  fi
}

current_worker_version() {
  "$PYTHON" - "$DEPLOYMENTS_JSON" <<'PY'
import json
import sys

deployments = json.load(open(sys.argv[1]))
if not isinstance(deployments, list) or not deployments:
    raise SystemExit("no prior Worker deployment is available for rollback")
latest = max(deployments, key=lambda item: str(item.get("created_on") or ""))
versions = latest.get("versions") or []
if not versions:
    raise SystemExit("latest Worker deployment has no versions")
selected = max(versions, key=lambda item: float(item.get("percentage") or 0))
version = str(selected.get("version_id") or "")
if not version:
    raise SystemExit("latest Worker deployment has no version ID")
print(version)
PY
}

audit_live_with_retry() {
  local attempt
  for attempt in 1 2 3; do
    if npm run audit:copy:live; then
      return 0
    fi
    if [ "$attempt" -lt 3 ]; then
      echo "Live audit attempt $attempt failed; retrying after 15 seconds" >&2
      sleep 15
    fi
  done
  return 1
}

if [ "$PLAN_ONLY" = 1 ]; then
  "$PYTHON" scripts/build_db.py
  "$PYTHON" scripts/ingest_cms_enforcement.py
  run_planner
  exit 0
fi

require_runtime
: "${R2_ACCOUNT_ID:?missing R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?missing R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?missing R2_SECRET_ACCESS_KEY}"
if [ "$NO_DEPLOY" != 1 ]; then
  wrangler whoami >/dev/null
fi

echo "Hospital Ledger cloud refresh $RUN_ID"
echo "workers: probe=$PROBE_WORKERS ingest=$INGEST_WORKERS r2=$R2_WORKERS"

"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" check --bucket "$BUCKET"
PRODUCTION_STARTED=1
write_run_status "running" "hydrating durable state"
upload_run_status

# Hydrate both durable pipeline state and every live last-known-good artifact.
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-file \
  "$BUCKET" "_pipeline/state/cloud_refresh_state.json" data/cloud_refresh_state.json --optional
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-file \
  "$BUCKET" "_pipeline/db/hospital_ledger.db" db/hospital_ledger.db --optional
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-file \
  "$BUCKET" "_pipeline/public/prices-index.json" public/data/prices/index.json --optional
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-file \
  "$BUCKET" "_pipeline/public/hospitals.json" public/data/hospitals.json --optional
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-file \
  "$BUCKET" "_pipeline/public/summary.json" public/data/summary.json --optional
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-prefix \
  "$BUCKET" "_pipeline/parsed/" data/parsed --optional
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-prefix \
  "$BUCKET" "prices/" public/data/prices
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-file \
  "$BUCKET" "indexes/cpt-index.json" public/data/cpt-index.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" download-prefix \
  "$BUCKET" "aggregates/" public/data --optional

if ! find data/parsed -maxdepth 1 -type f \( -name '*.json' -o -name '*.json.gz' \) -print -quit | grep -q .; then
  echo "No parsed checkpoint exists. Forcing a full rebuild from the live compact snapshot."
  BOOTSTRAP=1
  FORCE_ALL=1
fi

"$PYTHON" scripts/refresh_artifacts.py backup-public "$BASELINE_DIR"
"$PYTHON" scripts/build_db.py
"$PYTHON" scripts/ingest_cms_enforcement.py
run_planner

CHANGED_FILE="$ROOT/data/cloud_refresh_changed_ccns.txt"
WORKLIST_FILE="$ROOT/data/cloud_refresh_worklist.json"
PARSE_SUCCESS_FILE="$RUN_DIR/parse-success.txt"
SUCCESS_FILE="$RUN_DIR/publish-success.txt"
FAILURES_FILE="$ROOT/data/refresh_logs/ingest-$RUN_ID.failures.jsonl"
CHANGED_COUNT="$(wc -l < "$CHANGED_FILE" | tr -d ' ')"
echo "planned changed hospitals: $CHANGED_COUNT"

if [ "$CHANGED_COUNT" -eq 0 ]; then
  "$PYTHON" scripts/prepare_incremental_refresh.py --commit --processed-file "$CHANGED_FILE"
  "$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
    "$BUCKET" "_pipeline/db/hospital_ledger.db" db/hospital_ledger.db
  "$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
    "$BUCKET" "_pipeline/state/cloud_refresh_state.json" data/cloud_refresh_state.json
  write_run_status "no_changes" "all reachable MRF validators were unchanged"
  upload_run_status
  echo "No MRF content changed. Probe, database, and heartbeat checkpoints were updated."
  exit 0
fi

export MEM_GUARD_MAX_GB="${MEM_GUARD_MAX_GB:-8}"
export MEM_GUARD_MIN_FREE_GB="${MEM_GUARD_MIN_FREE_GB:-5}"
: > "$PARSE_SUCCESS_FILE"
: > "$SUCCESS_FILE"
: > "$FAILURES_FILE"

# The first bootstrap recreates roughly 107 GB of uncompressed parsed JSON but
# only about 5.8 GB once gzipped. Process bounded shards and slim each shard
# immediately so an ephemeral worker never needs room for the whole raw corpus.
SHARD_DIR="$RUN_DIR/ingest-shards"
mkdir -p "$SHARD_DIR"
split -l "${INGEST_SHARD_SIZE:-50}" -d -a 4 "$CHANGED_FILE" "$SHARD_DIR/shard-"
SHARD_TOTAL="$(find "$SHARD_DIR" -maxdepth 1 -type f -name 'shard-*' | wc -l | tr -d ' ')"
SHARD_DONE=0
for shard in "$SHARD_DIR"/shard-*; do
  [ -s "$shard" ] || continue
  shard_name="$(basename "$shard")"
  shard_parsed="$RUN_DIR/$shard_name-parsed.txt"
  shard_display="$RUN_DIR/$shard_name-display.txt"
  shard_status="$ROOT/data/refresh_logs/ingest-$RUN_ID-$shard_name.status.json"
  shard_backup="$BACKUP_DIR/$shard_name"

  "$PYTHON" scripts/refresh_artifacts.py backup "$shard" "$shard_backup"

  bash scripts/mem_guard.sh "$PYTHON" scripts/batch_ingest.py \
    --ccns-file "$shard" \
    --worklist "$WORKLIST_FILE" \
    --workers "$INGEST_WORKERS" \
    --item-timeout-seconds 1800 \
    --status-file "$shard_status" \
    --failures-file "$FAILURES_FILE"

  "$PYTHON" scripts/refresh_artifacts.py classify-parsed "$shard" "$shard_parsed"
  if [ -s "$shard_parsed" ]; then
    CCNS_FILE="$shard_parsed" SLIM_KEEP_STALE=1 SLIM_MERGE_INDEX=1 \
      bash scripts/mem_guard.sh "$PYTHON" scripts/slim_parsed.py
    "$PYTHON" scripts/refresh_artifacts.py classify-display "$shard_parsed" "$shard_display"
  else
    : > "$shard_display"
  fi
  "$PYTHON" scripts/refresh_artifacts.py restore-except "$shard" "$shard_display" "$shard_backup"
  cat "$shard_parsed" >> "$PARSE_SUCCESS_FILE"
  cat "$shard_display" >> "$SUCCESS_FILE"
  find "$shard_backup" -depth -delete 2>/dev/null || true
  SHARD_DONE=$((SHARD_DONE + 1))
  if [ $((SHARD_DONE % 5)) -eq 0 ] || [ "$SHARD_DONE" -eq "$SHARD_TOTAL" ]; then
    write_run_status "running" "processed ingest shard $SHARD_DONE of $SHARD_TOTAL"
    upload_run_status
  fi
done
sort -u -o "$PARSE_SUCCESS_FILE" "$PARSE_SUCCESS_FILE"
sort -u -o "$SUCCESS_FILE" "$SUCCESS_FILE"

PARSE_SUCCESS_COUNT="$(wc -l < "$PARSE_SUCCESS_FILE" | tr -d ' ')"
if [ "$PARSE_SUCCESS_COUNT" -eq 0 ]; then
  echo "No changed hospital produced a valid parsed record. Refusing publication." >&2
  exit 2
fi
SUCCESS_COUNT="$(wc -l < "$SUCCESS_FILE" | tr -d ' ')"
SUCCESS_PCT=$((100 * SUCCESS_COUNT / CHANGED_COUNT))
if [ -n "${MIN_SUCCESS_PCT:-}" ]; then
  REQUIRED_SUCCESS_PCT="$MIN_SUCCESS_PCT"
elif [ "$BOOTSTRAP" = 1 ]; then
  REQUIRED_SUCCESS_PCT="${BOOTSTRAP_MIN_SUCCESS_PCT:-85}"
else
  REQUIRED_SUCCESS_PCT=60
fi
echo "publishable changed hospitals: $SUCCESS_COUNT/$CHANGED_COUNT ($SUCCESS_PCT%, required $REQUIRED_SUCCESS_PCT%)"
if [ "$SUCCESS_COUNT" -eq 0 ] || [ "$SUCCESS_PCT" -lt "$REQUIRED_SUCCESS_PCT" ]; then
  echo "Too many changed hospitals lack displayable prices. Refusing publication." >&2
  exit 2
fi

# Rebuild global indexes from all durable parsed checkpoints. Existing compact
# files and index rows survive when a source is temporarily unavailable.
SLIM_KEEP_STALE=1 SLIM_MERGE_INDEX=1 \
  bash scripts/mem_guard.sh "$PYTHON" scripts/slim_parsed.py
"$PYTHON" scripts/refresh_artifacts.py reset-aggregate-dirs
bash scripts/mem_guard.sh "$PYTHON" scripts/promote_terminal_exceptions.py
bash scripts/mem_guard.sh "$PYTHON" scripts/build_aggregates.py
bash scripts/mem_guard.sh "$PYTHON" scripts/build_site_data.py
bash scripts/mem_guard.sh "$PYTHON" scripts/predeploy_audit.py --fix
bash scripts/mem_guard.sh "$PYTHON" scripts/predeploy_audit.py

VALIDATE_ARGS=(
  --baseline-summary "$BASELINE_DIR/summary.json"
  --processed-file "$SUCCESS_FILE"
  --max-regression-pct "$MAX_REGRESSION_PCT"
)
if [ -f "$BASELINE_DIR/compliance-ranking.json" ] && [ -f "$BASELINE_DIR/payers-index.json" ]; then
  VALIDATE_ARGS+=(--baseline-data-dir "$BASELINE_DIR")
fi
"$PYTHON" scripts/validate_refresh_output.py "${VALIDATE_ARGS[@]}"
npm run typecheck
npm run build

if [ "$NO_DEPLOY" = 1 ]; then
  write_run_status "validated_no_deploy" "local build and audits passed"
  upload_run_status
  echo "Local build and audits succeeded. Production publication was skipped."
  exit 0
fi

# Determine the complete live key set, including obsolete aggregate objects
# that must be deleted and therefore must also be restorable.
"$PYTHON" scripts/refresh_artifacts.py publication-keys "$SUCCESS_FILE" "$PUBLICATION_KEYS"
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" list-keys \
  "$BUCKET" "$REMOTE_AGGREGATE_KEYS" "aggregates/payer/" "aggregates/cpt-detail/"
sort -u "$PUBLICATION_KEYS" "$REMOTE_AGGREGATE_KEYS" > "$ROLLBACK_KEYS"
comm -23 "$REMOTE_AGGREGATE_KEYS" "$PUBLICATION_KEYS" > "$STALE_KEYS"

wrangler deployments list --json > "$DEPLOYMENTS_JSON"
PREVIOUS_WORKER_VERSION="$(current_worker_version)"

"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" snapshot-keys \
  "$BUCKET" "_pipeline/rollback/$RUN_ID" "$ROLLBACK_KEYS" "$ROLLBACK_MANIFEST"
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/rollback/$RUN_ID/manifest.json" "$ROLLBACK_MANIFEST"
R2_ROLLBACK_ARMED=1

"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-ccns \
  "$BUCKET" "prices/" public/data/prices "$SUCCESS_FILE" --extension .json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "prices/index.json" public/data/prices/index.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "indexes/cpt-index.json" public/data/cpt-index.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "aggregates/compliance-ranking.json" public/data/compliance-ranking.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "aggregates/payers-index.json" public/data/payers-index.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-prefix \
  "$BUCKET" "aggregates/payer/" public/data/payer --suffix .json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-prefix \
  "$BUCKET" "aggregates/cpt-detail/" public/data/cpt-detail --suffix .json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" delete-keys "$BUCKET" "$STALE_KEYS"

WORKER_ROLLBACK_ARMED=1
npm run deploy
audit_live_with_retry

# The new live data is verified. Subsequent checkpoint failures should alert
# and retry next run, but should not undo a valid publication.
WORKER_ROLLBACK_ARMED=0
R2_ROLLBACK_ARMED=0

"$PYTHON" scripts/prepare_incremental_refresh.py --commit --processed-file "$SUCCESS_FILE"
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-ccns \
  "$BUCKET" "_pipeline/parsed/" data/parsed "$SUCCESS_FILE" --extension .json.gz
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/db/hospital_ledger.db" db/hospital_ledger.db
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/public/prices-index.json" public/data/prices/index.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/public/hospitals.json" public/data/hospitals.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/public/summary.json" public/data/summary.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/public/cpt-index.json" public/data/cpt-index.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" upload-file \
  "$BUCKET" "_pipeline/state/cloud_refresh_state.json" data/cloud_refresh_state.json
"$PYTHON" scripts/r2_store.py --workers "$R2_WORKERS" prune-snapshots \
  "$BUCKET" "_pipeline/rollback" --retain 2

write_run_status "published" "live Worker and R2 publication passed validation"
upload_run_status

echo "Hospital Ledger refresh published and verified: $RUN_ID"
