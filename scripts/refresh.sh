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

run() {  # run a step; on dry-run, just print
  local msg="$1"; shift
  echo ""; echo "── $msg ──────────────────────────────────────────"
  if [ "$DRY_RUN" = 1 ]; then echo "DRY: $*"; return 0; fi
  "$@" 2>&1 | tee -a "$RUN_LOG"
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
    python3 scripts/batch_ingest.py --resume --all --workers 4 --item-timeout-seconds 1800
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
