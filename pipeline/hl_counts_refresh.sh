#!/bin/bash
# hl_counts_refresh.sh — Hospital Ledger weekly counts refresh, systemd-oneshot runner.
#
# Encodes the runbook (~/workspace/skills/hospital-ledger-refresh/SKILL.md +
# the weekly cron body) as a deterministic script so the multi-hour workflow
# survives detached from any cron worker run. Cron worker runs reap ALL
# descendant processes at run end (proven 2026-09-23); the 45-min worker
# timeout also kills phase 2 (3,609-URL probe needs ~65+ min). Hence oneshot.
#
# Run by: hl-counts-refresh.service (Type=oneshot), triggered by the weekly cron.
# Secrets: BEARER_SHIM_KEY (phase 0) and R2 creds (unit EnvironmentFile) are
# NEVER echoed. No `set -x` anywhere in this file.
#
# Exit codes (match runbook): 0 ok · 1 build/validate failed · 3 publish failed
# · 4 post-publish live check failed.
set -u

GOAL_DIR="/home/hatch/workspace/goals/hospital-ledger-weekly-refresh-counts-tier"
HIDDEN="$GOAL_DIR/hidden_files"
ROOT="/home/hatch/hospital-ledger"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
LOG="$ROOT/data/refresh_logs/counts-$TS.log"
mkdir -p "$ROOT/data/refresh_logs"
STATUS="$HIDDEN/counts-refresh-status.json"
TIMELINE="$HIDDEN/goal-timeline.md"
PRODUCER="${HL_PRODUCER:-muse.ai nova}"
OUT="${OUT:-$ROOT/data/site-out}"
CONCURRENCY="${CONCURRENCY:-12}"
VENV_PY="$ROOT/.venv/bin/python3"

exec > >(tee -a "$LOG") 2>&1
echo "[runner] counts refresh start ts=$TS producer=$PRODUCER concurrency=$CONCURRENCY"

GENERATED_AT=""
COMPLIANT_LINE=""
NEWLY_LIVE=""
NEWLY_DEAD=""
BEFORE_SNAP=""
GUARD_NOTE=""

status_write() {
  # status_write key=value ...  (values JSON-decoded when possible)
  python3 - "$STATUS" "$@" <<'PY'
import json, sys
p = sys.argv[1]
try:
    s = json.load(open(p))
except Exception:
    s = {}
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    try:
        s[k] = json.loads(v)
    except Exception:
        s[k] = v
s["updated_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
json.dump(s, open(p, "w"), indent=1)
PY
}

timeline() {
  printf '\n## %s — counts refresh (systemd)\n%s\n' "$(date -u +%Y-%m-%d\ %H:%M:%S\ UTC)" "$1" >> "$TIMELINE"
}

# ---------------------------------------------------------------- phase 0
phase0_self_update() {
  echo "[phase0] self-update check"
  # shellcheck disable=SC1091
  source /home/hatch/.config/muse-creds/env   # BEARER_SHIM_KEY
  local remote wm current
  wm="$HIDDEN/pipeline-sha256.txt"
  remote="$(curl -sf -m 30 -H "Authorization: Bearer ${BEARER_SHIM_KEY}" \
    https://authshim.barkleesanders.com/pull/pub/hospital-ledger-pipeline.sha256 | tr -d ' \n\r')"
  unset BEARER_SHIM_KEY
  [ -n "$remote" ] || { echo "[phase0] FAIL: could not fetch remote sha256"; return 1; }
  current=""; [ -f "$wm" ] && current="$(tr -d ' \n\r' < "$wm")"
  if [ "$remote" = "$current" ]; then
    echo "[phase0] bundle up to date ($remote)"
    return 0
  fi
  echo "[phase0] bundle changed $current -> $remote; re-pulling"
  local tgz="/tmp/hospital-ledger-pipeline.tar.gz"
  # shellcheck disable=SC1091
  source /home/hatch/.config/muse-creds/env
  curl -sf -m 600 -H "Authorization: Bearer ${BEARER_SHIM_KEY}" \
    -o "$tgz" https://authshim.barkleesanders.com/pull/pub/hospital-ledger-pipeline.tar.gz || { unset BEARER_SHIM_KEY; echo "[phase0] FAIL: tarball download"; return 1; }
  unset BEARER_SHIM_KEY
  echo "$remote  $tgz" | sha256sum -c - || { echo "[phase0] FAIL: tarball sha256 mismatch"; rm -f "$tgz"; return 1; }
  local env_before env_after
  env_before="$(sha256sum /home/hatch/hospital-ledger.env | cut -d' ' -f1)"
  tar --no-same-owner -xzf "$tgz" -C /home/hatch || { echo "[phase0] FAIL: extract"; rm -f "$tgz"; return 1; }
  env_after="$(sha256sum /home/hatch/hospital-ledger.env | cut -d' ' -f1)"
  if [ "$env_before" != "$env_after" ]; then
    echo "[phase0] FATAL: hospital-ledger.env changed by re-extract — aborting"
    rm -f "$tgz"; return 1
  fi
  printf '%s\n' "$remote" > "$wm"
  rm -f "$tgz"
  echo "[phase0] re-extracted bundle $remote; env checksum unchanged"
}

snapshot_before() {
  BEFORE_SNAP="$HIDDEN/before-$(date -u +%Y-%m-%d).json"
  # system python3 (not the venv): phase 0 may have re-extracted the bundle
  python3 - "$ROOT/public/data/hospitals.json" "$BEFORE_SNAP" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))
m = {x["ccn"]: bool(x.get("has_live_mrf")) for x in items if x.get("ccn")}
json.dump(m, open(sys.argv[2], "w"))
print("snapshot_before: %d ccns" % len(m))
PY
}

# ---------------------------------------------------------------- phase 1
phase1_env() {
  echo "[phase1] environment"
  [ -x "$VENV_PY" ] || python3 -m venv "$ROOT/.venv" || { echo "[phase1] FAIL: venv"; return 1; }
  "$ROOT/.venv/bin/python" -c "import httpx" 2>/dev/null || \
    "$ROOT/.venv/bin/python" -m pip install --quiet "httpx==0.28.1" || { echo "[phase1] FAIL: pip httpx"; return 1; }
  # httpx 0.28.1 crashes on IPv6 literals in NO_PROXY (InvalidURL: Invalid port: ':1]')
  strip_ipv6() { printf '%s' "$1" | tr ',' '\n' | grep -v ':' | paste -sd, - ; }
  [ -n "${NO_PROXY:-}" ] && export NO_PROXY="$(strip_ipv6 "$NO_PROXY")"
  [ -n "${no_proxy:-}" ] && export no_proxy="$(strip_ipv6 "$no_proxy")"
  local ver
  ver="$("$ROOT/.venv/bin/python" -c "import httpx; print(httpx.__version__)" 2>/dev/null)"
  [ "$ver" = "0.28.1" ] || { echo "[phase1] FAIL: httpx version=$ver"; return 1; }
  echo "[phase1] gate OK (httpx 0.28.1)"
}

# ---------------------------------------------------------------- phase 2
phase2_probe() {
  echo "[phase2] probe --all --concurrency $CONCURRENCY"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/probe_mrf_urls.py" --all --concurrency "$CONCURRENCY" || { echo "[phase2] FAIL: probe exit nonzero"; return 1; }
  grep -q "probe complete" "$LOG" || { echo "[phase2] FAIL: 'probe complete' not in log"; return 1; }
  echo "[phase2] gate OK"
}

# ---------------------------------------------------------------- phase 3
phase3_enforcement() {
  echo "[phase3] enforcement ingest"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/ingest_cms_enforcement.py" || { echo "[phase3] FAIL"; return 1; }
  echo "[phase3] gate OK"
}

# ---------------------------------------------------------------- phase 4
phase4_registry() {
  echo "[phase4] registry + summary"
  local marker="/tmp/counts-phase4-$TS.marker"
  touch "$marker"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/build_site_data.py" || { echo "[phase4] FAIL"; rm -f "$marker"; return 1; }
  [ "$ROOT/public/data/summary.json" -nt "$marker" ] && [ "$ROOT/public/data/hospitals.json" -nt "$marker" ] \
    || { echo "[phase4] FAIL: summary.json/hospitals.json not freshened"; rm -f "$marker"; return 1; }
  rm -f "$marker"
  echo "[phase4] gate OK"
}

# ---------------------------------------------------------------- phase 4.5
phase45_guard() {
  echo "[phase4.5] index-regression guard"
  "$VENV_PY" "$HIDDEN/hl_weekly_guard.py"
  local rc=$?
  case $rc in
    0) echo "[phase4.5] guard clean" ;;
    2) GUARD_NOTE="guard dropped CCN(s) (exit 2) — see log; publishing guarded index anyway"
       echo "[phase4.5] $GUARD_NOTE" ;;
    *) echo "[phase4.5] FAIL: guard operational failure (exit $rc) — STOP, do not publish"; return 1 ;;
  esac
}

# ---------------------------------------------------------------- phase 5
phase5_assemble() {
  echo "[phase5] assemble contract tree"
  rm -rf "$OUT"; mkdir -p "$OUT/meta" "$OUT/prices"
  cp "$ROOT/public/data/summary.json" "$OUT/meta/summary.json" || return 1
  cp "$ROOT/public/data/hospitals.json" "$OUT/meta/hospitals.json" || return 1
  cp "$ROOT/public/data/prices/index.json" "$OUT/prices/index.json" || return 1
  "$ROOT/.venv/bin/python" "$ROOT/scripts/make_manifest.py" "$OUT" --producer "$PRODUCER" --tier counts \
    --note "native refresh $TS" || { echo "[phase5] FAIL: make_manifest"; return 1; }
  [ -f "$OUT/meta/manifest.json" ] || { echo "[phase5] FAIL: manifest missing"; return 1; }
  echo "[phase5] gate OK"
}

# ---------------------------------------------------------------- phase 6
phase6_validate() {
  echo "[phase6] validate --tier counts"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/validate_site_data.py" "$OUT" --tier counts --quiet \
    || { echo "[phase6] FAIL: validation failed — STOP, nothing publishes"; return 1; }
  GENERATED_AT="$("$VENV_PY" -c "import json;print(json.load(open('$OUT/meta/summary.json'))['generated_at'])")"
  COMPLIANT_LINE="$("$VENV_PY" -c "import json;d=json.load(open('$OUT/meta/summary.json'));print('compliant:',d['compliant'],'/',d['cms_required_total'],'=',d['compliance_pct'],'%')")"
  echo "[phase6] gate OK generated_at=$GENERATED_AT $COMPLIANT_LINE"
}

# ---------------------------------------------------------------- phase 7
phase7_publish() {
  echo "[phase7] publish to R2"
  export R2_BUCKET="${PARSED_R2_BUCKET:-hl-mrf-parsed}"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/r2_put.py" --check || { echo "[phase7] FAIL: r2 --check"; return 1; }
  local k
  for k in prices/index.json meta/hospitals.json meta/summary.json meta/manifest.json; do
    "$ROOT/.venv/bin/python" "$ROOT/scripts/r2_put.py" "$OUT/$k" "$k" \
      || { echo "[phase7] FAIL: upload $k"; return 1; }
  done
  echo "[phase7] gate OK (manifest last)"
}

# ---------------------------------------------------------------- phase 8
phase8_livecheck() {
  echo "[phase8] live check"
  local i resp got src
  for i in $(seq 1 20); do
    resp="$(curl -sf -m 20 "https://hospitalledger.com/api/manifest?cb=$TS-$i" 2>/dev/null)" || { sleep 10; continue; }
    got="$(printf '%s' "$resp" | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('generated_at',''))" 2>/dev/null)"
    src="$(printf '%s' "$resp" | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('summary_source',''))" 2>/dev/null)"
    if [ "$got" = "$GENERATED_AT" ] && [ "$src" = "r2" ]; then
      echo "[phase8] gate OK (live generated_at matches, summary_source=r2)"
      return 0
    fi
    sleep 10
  done
  echo "[phase8] FAIL: live check — last generated_at=$got summary_source=$src (wanted $GENERATED_AT/r2)"
  return 1
}

compute_delta() {
  [ -n "$BEFORE_SNAP" ] && [ -f "$BEFORE_SNAP" ] || return 0
  local out
  out="$(python3 - "$BEFORE_SNAP" "$ROOT/public/data/hospitals.json" <<'PY'
import json, sys
before = json.load(open(sys.argv[1]))
items = json.load(open(sys.argv[2]))
after = {x["ccn"]: bool(x.get("has_live_mrf")) for x in items if x.get("ccn")}
nl = sum(1 for c, v in after.items() if v and not before.get(c))
nd = sum(1 for c, v in before.items() if v and not after.get(c))
print("%d %d" % (nl, nd))
PY
)"
  NEWLY_LIVE="${out%% *}"; NEWLY_DEAD="${out##* }"
  echo "[report] newly-live=$NEWLY_LIVE newly-dead=$NEWLY_DEAD"
}

run_all() {
  phase0_self_update || return 10
  snapshot_before   || return 11
  phase1_env        || return 21
  phase2_probe      || return 22
  phase3_enforcement|| return 23
  phase4_registry   || return 24
  phase45_guard     || return 25
  phase5_assemble   || return 26
  phase6_validate   || return 27
  phase7_publish    || return 28
  phase8_livecheck  || return 29
  return 0
}

map_exit() {
  case "$1" in
    0) echo 0 ;;
    27) echo 1 ;;   # validate failed
    28) echo 3 ;;   # publish failed
    29) echo 4 ;;   # live check failed
    *) echo 1 ;;
  esac
}

# ---------------------------------------------------------------- main
status_write run_ts="$TS" phase="starting" done=false reported=false exit_code=null \
  log_path="\"$LOG\"" generated_at=null compliant=null newly_live=null newly_dead=null \
  guard_note=null error=null

if [ "${SMOKE_TEST:-0}" = "1" ]; then
  echo "[runner] SMOKE_TEST=1 — phases 0-1 only"
  phase0_self_update && phase1_env
  rc=$?
  status_write phase="smoke" done=true exit_code="$rc" error="\"smoke test\""
  echo "[runner] smoke exit=$rc"
  exit "$rc"
fi

attempt=1
run_all; rc=$?
if [ "$rc" -ne 0 ] && [ "$attempt" -eq 1 ]; then
  echo "[runner] attempt 1 failed (code $rc) — retrying once per runbook"
  status_write phase="retry" error="\"attempt 1 failed code $rc; retrying\""
  attempt=2
  run_all; rc=$?
fi

EXIT_CODE="$(map_exit "$rc")"
compute_delta
status_write phase="finished" done=true exit_code="$EXIT_CODE" \
  generated_at="\"$GENERATED_AT\"" compliant="\"$COMPLIANT_LINE\"" \
  newly_live="\"$NEWLY_LIVE\"" newly_dead="\"$NEWLY_DEAD\"" \
  guard_note="\"$GUARD_NOTE\"" error="\"run_all code $rc (attempt $attempt)\""

timeline "- exit=$EXIT_CODE (run_all code $rc, attempt $attempt)
- log: $LOG
- generated_at: $GENERATED_AT
- $COMPLIANT_LINE
- newly-live MRF: $NEWLY_LIVE, newly-dead MRF: $NEWLY_DEAD
- guard: ${GUARD_NOTE:-clean}"

echo "[runner] finished exit=$EXIT_CODE (run_all code $rc, attempt $attempt)"
exit "$EXIT_CODE"
