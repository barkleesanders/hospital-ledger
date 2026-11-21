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
  # Muse's configured proxy measured 23-30+ seconds for this tiny response on
  # 2026-10-26. Bound retries, retain curl's safe transport/HTTP diagnostic,
  # and never confuse a timed-out download with an unchanged bundle.
  if ! remote="$(curl -fsS --connect-timeout 20 --max-time 90 \
    --retry 2 --retry-delay 3 --retry-max-time 240 \
    -H "Authorization: Bearer ${BEARER_SHIM_KEY}" \
    https://authshim.barkleesanders.com/pull/pub/hospital-ledger-pipeline.sha256)"; then
    unset BEARER_SHIM_KEY
    echo "[phase0] FAIL: checksum download failed (transport/HTTP diagnostic above)"
    return 1
  fi
  unset BEARER_SHIM_KEY
  remote="$(printf '%s' "$remote" | tr -d ' \n\r')"
  [[ "$remote" =~ ^[0-9a-f]{64}$ ]] || { echo "[phase0] FAIL: checksum response is not a SHA-256"; return 1; }
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
# Persistence doctrine (2026-11-15, Barklee directive): the probe is a
# resumable ledger, not a one-shot pass. Each item is tried by up to three
# DIFFERENT methods (edge -> mini -> retry); an item no method can answer
# is SKIPPED (recorded, prior data kept, never a death). Phase 2 completes
# when coverage is total: answered + skipped == seeded, pending == 0.
#
# Transport guard (2026-11-05, approval-card incident) — PRESERVED: on the
# VM the probe's primary lane is hl_edge; HL_FETCH_PROXY comes from
# hospital-ledger.env, loaded by the systemd unit's EnvironmentFile. A
# direct-mode probe from the VM raises one Muse approval card per
# first-seen hospital domain. If the edge env is missing, FAIL CLOSED
# (return 2 -> stop code 30): fix the unit env, never route around it.
# The second method IS the guard's sanctioned fallback (probe from the
# Mac mini; results recorded back into the VM DB); the third is a
# time-separated second edge pass.
# CARDING-LANE REMOVAL (2026-11-15, Barklee report): the resilient-egress
# CONNECT proxy (127.0.0.1:8899) was method 2 for one morning and carded
# 3 first-seen hospital domains in a single burst — a CONNECT proxy makes
# the VM process name every target domain to the gateway, which is
# exactly what the approval gate cards. It is removed from the ladder;
# never re-add any CONNECT-proxy or direct VM fetch for hospital domains.
RUN_ID="counts-$TS"
# Resume identity (2026-11-15, VM-reboot lesson): if the previous run's
# status record is unfinished (done != true) and carries a run_id, reuse
# it — the probe ledger is keyed by run_id, so a restarted service
# resumes the dead run's probe instead of re-probing from zero. A
# finished previous run (done=true) always starts a fresh run_id.
PREV_RUN_ID="$("$VENV_PY" - "$STATUS" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    if d.get("done") is not True and str(d.get("run_id") or "").startswith("counts-"):
        print(d["run_id"])
except Exception:
    pass
PY
)"
if [ -n "$PREV_RUN_ID" ] && [ "$PREV_RUN_ID" != "$RUN_ID" ]; then
  RUN_ID="$PREV_RUN_ID"
  echo "[runner] resuming unfinished run_id $RUN_ID (probe ledger carries over)"
fi
PROBE_ANSWERED=0; PROBE_SKIPPED=0; PROBE_PENDING=0

probe_counts() {
  local out
  out="$("$VENV_PY" - "$ROOT/db/hospital_ledger.db" "$RUN_ID" <<'PY'
import sqlite3, sys
try:
    conn = sqlite3.connect(sys.argv[1])
    rows = dict(conn.execute(
        "SELECT state, COUNT(*) FROM mrf_probe_run WHERE run_id=? GROUP BY state",
        (sys.argv[2],)).fetchall())
except Exception:
    rows = {}
print(rows.get('answered', 0), rows.get('skipped', 0), rows.get('pending', 0))
PY
)"
  PROBE_ANSWERED="${out%% *}"; PROBE_PENDING="${out##* }"
  PROBE_SKIPPED="$(printf '%s' "$out" | cut -d' ' -f2)"
  echo "[phase2] coverage answered=$PROBE_ANSWERED skipped=$PROBE_SKIPPED pending=$PROBE_PENDING"
}

phase2_probe() {
  echo "[phase2] probe run_id=$RUN_ID concurrency=$CONCURRENCY (methods: edge -> mini -> retry)"
  if [ -z "${HL_FETCH_PROXY:-}" ]; then
    echo "[phase2] FAIL: HL_FETCH_PROXY unset — edge leg (sanctioned primary) unavailable; fail closed (2026-11-05 guard)"
    return 2
  fi
  # A method's operational failure never aborts the ladder: it is recorded
  # and the next method tries the still-pending items (2026-11-15 smoke
  # catch — an edge preflight timeout would otherwise starve methods 2-3).
  local method_errors=0
  "$VENV_PY" "$ROOT/scripts/probe_mrf_urls.py" --all \
    --concurrency "$CONCURRENCY" --run-id "$RUN_ID" --method edge \
    || { method_errors=$((method_errors + 1)); echo "[phase2] probe (edge) operational error — continuing the ladder"; }
  probe_counts
  if [ "$PROBE_PENDING" -eq 0 ]; then echo "[phase2] gate OK (edge)"; return 0; fi
  echo "[phase2] $PROBE_PENDING item(s) unanswered by edge — method 2: mini leg"
  env -u HL_FETCH_PROXY "$VENV_PY" "$ROOT/scripts/probe_mrf_urls.py" --all \
    --concurrency "$CONCURRENCY" --run-id "$RUN_ID" --method mini \
    || { method_errors=$((method_errors + 1)); echo "[phase2] probe (mini) operational error — continuing the ladder"; }
  probe_counts
  if [ "$PROBE_PENDING" -eq 0 ]; then echo "[phase2] gate OK (mini)"; return 0; fi
  echo "[phase2] $PROBE_PENDING item(s) unanswered by edge+mini — method 3: retry (second edge pass, time-separated)"
  "$VENV_PY" "$ROOT/scripts/probe_mrf_urls.py" --all \
    --concurrency "$CONCURRENCY" --run-id "$RUN_ID" --method retry \
    || { method_errors=$((method_errors + 1)); echo "[phase2] probe (retry) operational error — continuing the ladder"; }
  probe_counts
  [ "$PROBE_PENDING" -eq 0 ] || { echo "[phase2] FAIL: coverage incomplete after all three methods"; return 1; }
  echo "[phase2] gate OK (all methods; skipped=$PROBE_SKIPPED recorded in ledger, prior data kept)"
  if [ "$PROBE_SKIPPED" -gt 0 ]; then
    local total=$((PROBE_ANSWERED + PROBE_SKIPPED))
    echo "[phase2] NOTE: $PROBE_SKIPPED/$total item(s) skipped after 3 methods — no-answer items, NOT deaths"
    if [ "$PROBE_ANSWERED" -eq 0 ]; then
      # Distinct return 2 -> run_all code 30 (STOP class): every leg dead for
      # every item is a transport incident to escalate, not a run to loop on —
      # the ledger is terminal for this run_id and a retry cannot change it.
      # The next weekly cycle re-probes under a fresh run_id.
      echo "[phase2] FAIL: zero answered — every leg is dead; transport incident, not data"
      return 2
    fi
  fi
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
  # Merge fresh compliance counts into live Tier-3 summary (2026-09-27 fix:
  # the local ~/hospital-ledger/ working copy is stale pre-Tier-3; blindly
  # copying its summary.json overwrote 3,699 hospitals with 26).
  local live_summary="/tmp/counts-live-summary-$TS.json"
  if ! "$VENV_PY" "$ROOT/scripts/r2_put.py" --get-file "meta/summary.json" "$live_summary" 2>/dev/null; then
    echo "[phase5] FAIL: cannot fetch live meta/summary.json from R2 — refusing to overwrite Tier-3 data"
    return 1
  fi
  "$VENV_PY" "$HIDDEN/../hospital-ledger-push/pipeline/merge_summary.py" \
    "$ROOT/public/data/summary.json" "$live_summary" "$OUT/meta/summary.json" \
    || { echo "[phase5] FAIL: merge_summary"; return 1; }
  rm -f "$live_summary"
  cp "$ROOT/public/data/hospitals.json" "$OUT/meta/hospitals.json" || return 1
  # DO NOT copy the stale local prices/index.json (26 entries) — the Tier-3
  # index (3,810) is canonical in R2; the counts refresh does not own it.
  # Fetch the live index to keep the manifest hash consistent.
  if ! "$VENV_PY" "$ROOT/scripts/r2_put.py" --get-file "prices/index.json" "$OUT/prices/index.json" 2>/dev/null; then
    echo "[phase5] FAIL: cannot fetch live prices/index.json from R2"
    return 1
  fi
  "$VENV_PY" "$ROOT/scripts/site_data_metrics.py" "$OUT" \
    || { echo "[phase5] FAIL: registry/index cohort counts"; return 1; }
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
  local k attempt
  for k in prices/index.json meta/hospitals.json meta/summary.json meta/manifest.json; do
    # Per-key bounded retry (2026-10-23): the 2026-10-18 run died here when
    # the egress proxy dropped the meta/summary.json PUT mid-sequence --
    # index + hospitals landed, the manifest (last) never did, and R2 was
    # left inconsistent, deadlocking the daily pricing wave for 5 days.
    # PUTs are idempotent (same bytes, same key), so retry the FAILED key
    # in place; never restart the sequence, never skip ahead -- manifest
    # stays last.
    for attempt in 1 2 3; do
      "$ROOT/.venv/bin/python" "$ROOT/scripts/r2_put.py" "$OUT/$k" "$k" && break
      echo "[phase7] upload $k attempt $attempt failed"
      [ "$attempt" = 3 ] && { echo "[phase7] FAIL: upload $k (3 attempts)"; return 1; }
      sleep 20
    done
  done
  echo "[phase7] gate OK (manifest last)"
}

# ---------------------------------------------------------------- phase 8
phase8_livecheck() {
  echo "[phase8] live check"
  # FIXED 2026-11-15: this gate previously polled /api/manifest for a
  # top-level "summary_source" field that NO producer writes (grep the
  # pipeline: the string existed only in this check and in the retired
  # scripts/refresh_counts.sh) — the gate could never pass, and two
  # consecutive runs failed phase 8 (code 29) on correctly published
  # data. The source fact lives in /api/coverage, which returns
  # {generated_at, source} for the summary the site is actually
  # serving. The gate's intent is unchanged: the live site serves the
  # just-published summary, sourced from R2.
  local i resp got src
  for i in $(seq 1 20); do
    resp="$(curl -sf -m 20 "https://hospitalledger.com/api/coverage?cb=$TS-$i" 2>/dev/null)" || { sleep 10; continue; }
    got="$(printf '%s' "$resp" | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('generated_at',''))" 2>/dev/null)"
    src="$(printf '%s' "$resp" | "$VENV_PY" -c "import json,sys;print(json.load(sys.stdin).get('source',''))" 2>/dev/null)"
    if [ "$got" = "$GENERATED_AT" ] && [ "$src" = "r2" ]; then
      echo "[phase8] gate OK (live generated_at matches, source=r2)"
      return 0
    fi
    sleep 10
  done
  echo "[phase8] FAIL: live check — last generated_at=$got source=$src (wanted $GENERATED_AT/r2)"
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
  phase2_probe; local p2rc=$?
  if [ "$p2rc" -eq 2 ]; then return 30; elif [ "$p2rc" -ne 0 ]; then return 22; fi
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
# Single-instance: a still-running refresh (the keep-trying loop below can
# legitimately run long) makes a duplicate trigger a no-op, never a race.
exec 9>"$HIDDEN/counts-refresh.lock"
if ! flock -n 9; then
  echo "[runner] another counts refresh holds the lock — exiting (no-op)"
  exit 0
fi

status_write run_ts="$TS" run_id="\"$RUN_ID\"" phase="starting" done=false reported=false exit_code=null \
  log_path="\"$LOG\"" generated_at=null compliant=null newly_live=null newly_dead=null \
  guard_note=null error=null probe_answered=null probe_skipped=null

if [ "${SMOKE_TEST:-0}" = "1" ]; then
  echo "[runner] SMOKE_TEST=1 — phases 0-1 only"
  phase0_self_update && phase1_env
  rc=$?
  status_write phase="smoke" done=true exit_code="$rc" error="\"smoke test\""
  echo "[runner] smoke exit=$rc"
  exit "$rc"
fi

# Persistence doctrine (2026-11-15, Barklee directive — supersedes the old
# retry-once rule): TRANSIENT failures (phase 0 transport 10, probe
# operational 22, publish 28, live-check lag 29) resume the whole run until
# it completes — phases are idempotent and phase 2 resumes from its ledger,
# so a retry never redoes finished work. DATA-CLASS failures (env 21,
# enforcement 23, build 24, guard 25, assemble 26, validate 27) and the
# stop-class transport/environment incidents (30) STOP the run: the run
# record + status file carry the shape, and the refresh skill's auto-tutor
# step engages on it instead of a bare retry or a bare escalation.
is_transient() { case "$1" in 10|22|28|29) return 0 ;; *) return 1 ;; esac }

attempt=1
while : ; do
  run_all; rc=$?
  [ "$rc" -eq 0 ] && break
  if ! is_transient "$rc"; then
    echo "[runner] stop-class failure (code $rc, attempt $attempt) — run stops; auto-tutor/escalation engages from the record"
    break
  fi
  echo "[runner] transient failure (code $rc, attempt $attempt) — resuming in 300s (retry until complete)"
  status_write phase="retry" attempt="$attempt" error="\"transient code $rc; resuming\""
  attempt=$((attempt + 1))
  sleep 300
done

EXIT_CODE="$(map_exit "$rc")"
compute_delta
probe_counts >/dev/null 2>&1 || true
status_write phase="finished" done=true exit_code="$EXIT_CODE" \
  generated_at="\"$GENERATED_AT\"" compliant="\"$COMPLIANT_LINE\"" \
  newly_live="\"$NEWLY_LIVE\"" newly_dead="\"$NEWLY_DEAD\"" \
  guard_note="\"$GUARD_NOTE\"" error="\"run_all code $rc (attempt $attempt)\"" \
  probe_answered="$PROBE_ANSWERED" probe_skipped="$PROBE_SKIPPED"

timeline "- exit=$EXIT_CODE (run_all code $rc, attempt $attempt)
- log: $LOG
- generated_at: $GENERATED_AT
- $COMPLIANT_LINE
- newly-live MRF: $NEWLY_LIVE, newly-dead MRF: $NEWLY_DEAD
- guard: ${GUARD_NOTE:-clean}"

echo "[runner] finished exit=$EXIT_CODE (run_all code $rc, attempt $attempt)"
exit "$EXIT_CODE"
