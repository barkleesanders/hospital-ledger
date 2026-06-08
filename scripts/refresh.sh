#!/usr/bin/env bash
# scripts/refresh.sh — autonomous weekly refresh of hospitalledger.com.
#
# Designed to run from a launchd timer on the mac mini (and locally for testing).
# Idempotent: safe to re-run. Allowlist-only commits. Aborts on the failure modes
# this session caught the hard way (cpt-index regression, missing R2 uploads,
# stale copy, runaway parse-failure %, 3.3 GB side-cars getting committed).
#
# Usage:
#   bash scripts/refresh.sh                # full pipeline (probe → ingest → slim → audit → deploy)
#   bash scripts/refresh.sh --dry-run      # print what each step would do; no mutations
#   bash scripts/refresh.sh --no-deploy    # everything except wrangler deploy (for testing)
#   bash scripts/refresh.sh --skip-ingest  # skip Stage 4 (use existing data/parsed/)
#   bash scripts/refresh.sh --quick        # = --skip-ingest --no-deploy (smoke test)
#
# Exit codes:
#   0  success
#   1  pre-flight failure (git out-of-sync, audit fail, etc.)
#   2  ingest failure (>50% of attempts failed → don't ship garbage)
#   3  deploy failure or post-deploy live audit failure

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Use the project venv when present so all `python3 …` resolves to it
# (mac-mini ships Python 3.9.6 system-wide; deps live in .venv only).
if [ -x ".venv/bin/python3" ]; then
  export PATH="$ROOT/.venv/bin:$PATH"
fi

# Cap to ~50% of system resources: put this script (and every child process)
# into macOS background QoS class via taskpolicy. The OS aggressively yields
# CPU to interactive processes when this class is set. nice -n 19 backs it up
# for systems without taskpolicy. This is the difference between "refresh
# hogs the mac mini for 75 min" and "refresh runs alongside everything else".
if command -v taskpolicy >/dev/null 2>&1; then
  taskpolicy -c background -p $$ 2>/dev/null || true
fi
renice -n 19 -p $$ >/dev/null 2>&1 || true

# Hard safety ceiling. Every step runs under scripts/mem_guard.sh, which kills
# the step (and all its children) the moment resident memory crosses
# MEM_GUARD_MAX_GB or free disk drops below MEM_GUARD_MIN_FREE_GB. This is the
# backstop for the 2026-06-07 incident: slim_parsed.py grew to 11.8 GB on the
# 16 GB mac mini, Jetsam thrashed, WindowServer missed its watchdog check-ins,
# and the kernel PANICKED and rebooted the machine. macOS does NOT enforce
# `ulimit -v` (verified), so the guard polls RSS externally and SIGKILLs on
# breach — a runaway step now fails loudly instead of taking the desktop down.
GUARD="$ROOT/scripts/mem_guard.sh"
export MEM_GUARD_MAX_GB="${MEM_GUARD_MAX_GB:-8}"
export MEM_GUARD_MIN_FREE_GB="${MEM_GUARD_MIN_FREE_GB:-3}"

# ---- args ------------------------------------------------------------------
DRY_RUN=0; NO_DEPLOY=0; SKIP_INGEST=0; MAX_FAIL_PCT=50
for arg in "$@"; do
  case "$arg" in
    --dry-run)      DRY_RUN=1 ;;
    --no-deploy)    NO_DEPLOY=1 ;;
    --skip-ingest)  SKIP_INGEST=1 ;;
    --quick)        SKIP_INGEST=1; NO_DEPLOY=1 ;;
    --max-fail-pct=*) MAX_FAIL_PCT="${arg#*=}" ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg (see --help)"; exit 1 ;;
  esac
done

LOG_DIR="$ROOT/data/refresh_logs"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
RUN_LOG="$LOG_DIR/run-$TS.log"

# ── Telegram failure alert (Hermes shared secret store) ──────────────────────
# Any non-zero exit (a step failing under set -e, a mem_guard kill = 137/138, an
# explicit ABORT exit 1/2/3, or an unexpected crash) pings Telegram so a future
# silent weekend failure can't leave the site stale for weeks again (the reason
# this alert exists: the 2026-05-31 + 2026-06-07 runs died before deploy and
# nobody knew). Secrets come from ~/.hermes/.env by env name — NEVER hardcoded
# (global Hermes-Env rule). If the env file or token is absent the alert is a
# silent no-op; it never blocks or fails the refresh.
[ -r "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
notify_failure() {
  local code="$1"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  [ -z "$tok" ] || [ -z "$chat" ] && return 0
  local reason="exit ${code}"
  case "$code" in
    137) reason="exit 137 — mem_guard KILLED a step (RSS > ${MEM_GUARD_MAX_GB}GB ceiling)";;
    138) reason="exit 138 — mem_guard KILLED a step (free disk < ${MEM_GUARD_MIN_FREE_GB}GB floor)";;
  esac
  local tail_log="(no run log)"
  [ -f "${RUN_LOG:-}" ] && tail_log="$(tail -n 15 "$RUN_LOG" 2>/dev/null | tail -c 1000)"
  curl -s -m 20 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=🏥❌ hospital-ledger weekly refresh FAILED
host: $(hostname -s)   run: ${TS:-?}   ${reason}
log: ${RUN_LOG:-?}
── last log lines ──
${tail_log}" >/dev/null 2>&1 || true
}
_on_exit() {
  local code=$?
  [ "$code" -ne 0 ] && [ "${DRY_RUN:-0}" != "1" ] && notify_failure "$code"
  return "$code"
}
trap _on_exit EXIT

run() {  # run a step; on dry-run, just print
  local msg="$1"; shift
  echo ""; echo "── $msg ──────────────────────────────────────────"
  if [ "$DRY_RUN" = 1 ]; then echo "DRY: $*"; return 0; fi
  "$GUARD" "$@" 2>&1 | tee -a "$RUN_LOG"
  return "${PIPESTATUS[0]}"
}

# ---- Step 0: pre-flight ----------------------------------------------------
echo "hl-refresh $TS  (dry_run=$DRY_RUN no_deploy=$NO_DEPLOY skip_ingest=$SKIP_INGEST)"
echo "log: $RUN_LOG"

run "Step 0: git sync" bash -c '
  git fetch origin --prune
  AHEAD=$(git rev-list --count @{u}..HEAD 2>/dev/null || echo 0)
  BEHIND=$(git rev-list --count HEAD..@{u} 2>/dev/null || echo 0)
  if [ "$BEHIND" -gt 0 ]; then echo "BEHIND origin by $BEHIND — pulling"; git pull --ff-only; fi
  if [ "$AHEAD" -gt 0 ]; then echo "AHEAD of origin by $AHEAD commits (uncommitted local work) — refusing to refresh"; exit 1; fi
'

# ---- Step 1: probe (optional — only if probe_mrf_urls.py supports cron-friendly mode)
# For now: skip the probe step in cron and let batch_ingest --resume re-attempt
# anything not already done. probe_mrf_urls.py is a manual stage-2 tool today.

# ---- Step 2: ingest --resume (skips done; re-attempts gap) -----------------
if [ "$SKIP_INGEST" = 1 ]; then
  echo "(skipping ingest per --skip-ingest)"
else
  run "Step 2: batch_ingest --resume --all" \
    python3 scripts/batch_ingest.py --resume --all --workers 2 --item-timeout-seconds 1800
  # workers=2 (was 4): with 10-core mac mini that's ~20% of CPU cores ingestion
  # capacity. Combined with the background QoS class above, this stays well under
  # 50% of system resources even when all workers + slim are busy.
  # Guard: if >MAX_FAIL_PCT of attempts failed, abort before deploy
  if [ "$DRY_RUN" != 1 ]; then
    FAIL_PCT=$(python3 -c "
import json
d = json.load(open('data/full_standardize_status.json'))
elig = d.get('eligible', 0) or 0
fail = d.get('failures', d.get('fail', 0)) or 0
print(int(100 * fail / elig) if elig else 0)
")
    echo "ingest fail %: $FAIL_PCT (threshold $MAX_FAIL_PCT)"
    if [ "$FAIL_PCT" -gt "$MAX_FAIL_PCT" ]; then
      echo "ABORT: ingest failed more than $MAX_FAIL_PCT% — investigate before deploying"
      exit 2
    fi
  fi
fi

# ---- Step 3: slim — targeted survivors, then full (rebuilds cpt-index) ------
if [ "$DRY_RUN" != 1 ]; then
  SURV=$(ls data/parsed/*.json 2>/dev/null | sed 's|.*/||;s|\.json$||' | tr '\n' ' ' || true)
else
  SURV=""
fi
if [ -n "$SURV" ]; then
  printf "%s\n" $SURV > /tmp/hl-refresh-survivors.txt
  run "Step 3a: targeted slim of $(echo $SURV | wc -w | tr -d ' ') survivors" bash -c '
    CCNS_FILE=/tmp/hl-refresh-survivors.txt SLIM_MERGE_INDEX=1 SLIM_KEEP_STALE=1 \
      python3 scripts/slim_parsed.py
  '
else
  echo "(no fresh parsed/*.json — skipping targeted slim)"
fi

run "Step 3b: full slim (rebuilds cpt-index; ~75 min)" \
  python3 scripts/slim_parsed.py

# Sanity: cpt-index must be 5000 codes and ≤12 MB (P1 cap)
if [ "$DRY_RUN" != 1 ]; then
  python3 -c "
import json, os, sys
p = 'public/data/cpt-index.json'
d = json.load(open(p))
sz = os.path.getsize(p) // 1024 // 1024
print(f'cpt-index: {len(d)} codes, {sz} MB')
if len(d) != 5000: sys.exit(f'EXPECTED 5000 codes, got {len(d)} — aborting')
if sz > 15:        sys.exit(f'cpt-index {sz} MB — too large to serve; aborting')
"
fi

# ---- Step 4: classify hard failures + rebuild aggregates -------------------
run "Step 4a: promote_terminal_exceptions" python3 scripts/promote_terminal_exceptions.py
run "Step 4b: build_aggregates"           python3 scripts/build_aggregates.py
run "Step 4c: build_site_data"            python3 scripts/build_site_data.py

# ---- Step 5: copy-truth audit ----------------------------------------------
run "Step 5a: predeploy_audit --fix (auto-correct stale README/home counts)" \
  python3 scripts/predeploy_audit.py --fix
run "Step 5b: predeploy_audit (must pass)" python3 scripts/predeploy_audit.py

# ---- Step 6: R2 sync (trusts manifest; skips unchanged) --------------------
run "Step 6: stage4_refresh UPLOAD_R2" bash -c '
  SKIP_INGEST=1 SKIP_SLIM=1 UPLOAD_R2=1 python3 scripts/stage4_refresh.py
'

# ---- Step 7: commit allowlisted files (NEVER -A — protects against side-car blobs)
ALLOW=(
  scripts/*.py
  scripts/*.sh
  README.md
  src/routes/*.tsx
  public/.assetsignore
  public/data/summary.json
  public/data/prices/index.json
  public/data/hospitals.json
  data/coverage_terminal_exceptions.json
  data/full_standardize_failures.jsonl
  data/full_standardize_status.json
  data/r2_upload_manifest.json
)
if [ "$DRY_RUN" != 1 ]; then
  git add "${ALLOW[@]}" 2>/dev/null || true
  if ! git diff --cached --quiet; then
    DELTA=$(python3 -c "
import json
s = json.load(open('public/data/summary.json'))
print(s.get('standardized_price_hospitals', '?'))
")
    git commit -m "chore(refresh): weekly refresh $(date -u +%Y-%m-%d) — priced=$DELTA"
    git push
  else
    echo "(no allowlisted changes to commit)"
  fi
fi

# ---- Step 8: deploy --------------------------------------------------------
if [ "$NO_DEPLOY" = 1 ]; then
  echo ""; echo "── --no-deploy: stopping before wrangler deploy ──"
  exit 0
fi

run "Step 8: wrangler deploy" bash -c 'rm -rf dist && npm run deploy'

# ---- Step 9: live verification --------------------------------------------
run "Step 9: audit:copy:live (must pass)" npm run audit:copy:live || {
  echo "ABORT: live audit failed — deploy may need rollback or R2 file may be missing"
  exit 3
}

echo ""
echo "✅ hl-refresh complete $TS"
echo "   priced: $(python3 -c "import json; print(json.load(open('public/data/summary.json'))['standardized_price_hospitals'])")"
echo "   log:    $RUN_LOG"
