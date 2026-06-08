#!/usr/bin/env bash
# scripts/mem_guard.sh — run a command under a hard resident-memory + free-disk
# ceiling, killing it (and all descendants) if either is breached.
#
# WHY THIS EXISTS (2026-06-07 incident):
#   The weekly refresh's `slim_parsed.py` step grew to 11.8 GB RSS on the 16 GB
#   mac mini. macOS Jetsam started killing processes (04:59–05:03), WindowServer
#   missed its userspace-watchdog check-ins for 121 s, and the kernel PANICKED
#   and force-rebooted the machine at 05:47. A single runaway batch job took the
#   whole Mac down.
#
#   We CANNOT rely on `ulimit -v` (RLIMIT_AS): macOS does NOT enforce it —
#   verified 2026-06-07, Python allocated 4 GB under a 1 GB `ulimit -v` cap with
#   no error. So we poll resident memory externally and SIGKILL on breach. A
#   killed step fails loudly (clean non-zero exit + log line) instead of taking
#   the desktop with it.
#
# USAGE:
#   scripts/mem_guard.sh <command> [args...]
#
# ENV KNOBS (all optional):
#   MEM_GUARD_MAX_GB        resident ceiling for the whole process tree (default 8)
#   MEM_GUARD_MIN_FREE_GB   abort if free disk drops below this (default 4)
#   MEM_GUARD_POLL_SECS     poll interval (default 5)
#   MEM_GUARD_DISABLE=1     bypass the guard entirely (run the command bare)
#
# EXIT CODES:
#   <command's own exit code>  normal completion
#   137  killed: resident memory exceeded MEM_GUARD_MAX_GB
#   138  killed: free disk fell below MEM_GUARD_MIN_FREE_GB
set -uo pipefail

if [ "${MEM_GUARD_DISABLE:-0}" = "1" ]; then
  exec "$@"
fi

MAX_GB="${MEM_GUARD_MAX_GB:-8}"
MIN_FREE_GB="${MEM_GUARD_MIN_FREE_GB:-4}"
POLL="${MEM_GUARD_POLL_SECS:-5}"
MAX_KB=$(( MAX_GB * 1024 * 1024 ))
MIN_FREE_KB=$(( MIN_FREE_GB * 1024 * 1024 ))

if [ "$#" -eq 0 ]; then
  echo "mem_guard: no command given" >&2
  exit 2
fi

# Collect the pid tree rooted at $1 (root + all descendants), breadth-first.
tree_pids() {
  local frontier="$1" all="$1" kids
  while [ -n "$frontier" ]; do
    kids="$(pgrep -P "$(echo "$frontier" | tr ' ' ',')" 2>/dev/null | tr '\n' ' ')"
    frontier="$kids"
    [ -n "$kids" ] && all="$all $kids"
  done
  echo "$all"
}

# Sum resident KB across a pid list.
sum_rss_kb() {
  local total=0 rss
  for p in $1; do
    rss="$(ps -o rss= -p "$p" 2>/dev/null | tr -d ' ')"
    [ -n "$rss" ] && total=$(( total + rss ))
  done
  echo "$total"
}

free_kb() { df -k "$PWD" | awk 'NR==2 {print $4}'; }

kill_tree() {
  local pids="$1"
  # TERM first for a chance to flush, then KILL.
  for p in $pids; do kill -TERM "$p" 2>/dev/null; done
  sleep 2
  for p in $pids; do kill -KILL "$p" 2>/dev/null; done
}

"$@" &
CMD_PID=$!

while kill -0 "$CMD_PID" 2>/dev/null; do
  pids="$(tree_pids "$CMD_PID")"
  rss="$(sum_rss_kb "$pids")"
  freek="$(free_kb)"

  if [ "${rss:-0}" -gt "$MAX_KB" ]; then
    echo "mem_guard: BREACH resident=${rss}KB > ceiling=${MAX_KB}KB (${MAX_GB}GB) — killing tree [$pids]" >&2
    kill_tree "$pids"
    wait "$CMD_PID" 2>/dev/null
    exit 137
  fi
  if [ "${freek:-999999999}" -lt "$MIN_FREE_KB" ]; then
    echo "mem_guard: BREACH free_disk=${freek}KB < floor=${MIN_FREE_KB}KB (${MIN_FREE_GB}GB) — killing tree [$pids]" >&2
    kill_tree "$pids"
    wait "$CMD_PID" 2>/dev/null
    exit 138
  fi
  sleep "$POLL"
done

wait "$CMD_PID"
exit $?
