#!/usr/bin/env python3
"""VM rebuild coordinator: pipe-aware sequential wave driver + merge.

- Probes R2 pipe health before each wave (single small GET, 90s budget).
- Runs vm_wave.py per wave as a foreground subprocess (collected, not fire-and-forget).
- Auto-fix ladder (Barklee 2026-09-24 directive: auto-fix everything, no
  operator round-trip, until all 11 waves complete):
    * wave fails on staging only + workdir intact -> --resume-stage
      (verify-first re-stage, skips objects the failed attempt got right);
    * any other failure -> full wave re-run;
    * back off 15 min between attempts;
    * HALT only after 5 consecutive failures of the same wave, with a
      precise halt_reason. The setup watchdog pages on a real halt.
- After wave 11 passes: runs vm_merge.py as a foreground subprocess.
- Heartbeat to vm-rebuild-progress.log every 15 min.
- Durable cursor + per-wave attempt budget in wave-orchestrator-status.json
  (owned by vm_wave.py).
"""
import datetime
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HF = HERE  # canonical progress log lives beside the wave plan
WAVES_DIR = os.path.join(os.path.expanduser("~"), "hl-vm-waves")
STATUS = os.path.join(HERE, "wave-orchestrator-status.json")
RESULTS_DIR = os.path.join(WAVES_DIR, "results")
PROBE_TIMEOUT = 90
RETRY_SLEEP = 900  # 15 min
HEARTBEAT_S = 900
MAX_ATTEMPTS = 5  # per-wave auto-fix budget before a real halt


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(msg):
    line = f"[{utcnow()}] [coordinator] {msg}"
    print(line, flush=True)
    with open(os.path.join(HF, "vm-rebuild-progress.log"), "a") as f:
        f.write(line + "\n")


def pipe_healthy():
    """True if a single small R2 GET completes within PROBE_TIMEOUT."""
    env = dict(os.environ)
    env["R2_BUCKET"] = "hl-mrf-parsed"
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "vm_r2client.py"),
             "get", "meta/manifest.json", "/tmp/vm-pipe-probe.json"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, env=env)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def run_wave(w, mode="full"):
    env = dict(os.environ)
    # 8 workers: the egress proxy penalty-boxes CONNECT bursts; 8 concurrent
    # (the archive driver's proven level) with 3s stagger gaps stays safe.
    workers = "8"
    args = [sys.executable, os.path.join(HERE, "vm_wave.py"), str(w)]
    if mode == "resume-stage":
        args += ["--resume-stage"]
    args += ["--workers", workers]
    log(f"launching wave {w} ({workers} workers, mode={mode})")
    p = subprocess.Popen(
        args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    # Stream heartbeats while the wave runs; collect at the end.
    last_beat = time.time()
    while True:
        time.sleep(30)
        if p.poll() is not None:
            break
        if time.time() - last_beat >= HEARTBEAT_S:
            log(f"wave {w} still running (pid {p.pid})")
            last_beat = time.time()
    out = p.stdout.read()
    tail = "\n".join(out.splitlines()[-15:])
    log(f"wave {w} exited rc={p.returncode}\ntail:\n{tail}")
    return p.returncode == 0


def wave_fail_reason(w):
    """Machine-readable failure reason from the wave's result file."""
    try:
        res = json.load(open(os.path.join(RESULTS_DIR, f"w{w}.json")))
        if res.get("verdict") == "fail":
            return res.get("reason", "unknown")
    except Exception:
        pass
    return "unknown"


def staging_resumable(w):
    """True iff the failed wave's workdir can be re-staged without a re-run."""
    wdir = os.path.join(WAVES_DIR, f"w{w}")
    state = os.path.join(wdir, "stage-state.json")
    prices = os.path.join(wdir, "public", "data", "prices")
    return (os.path.exists(state) and os.path.isdir(prices)
            and any(f.endswith(".json") for f in os.listdir(prices)))


def main():
    os.makedirs(WAVES_DIR, exist_ok=True)
    log("coordinator start")
    while True:
        s = json.load(open(STATUS))
        if s.get("halted"):
            log("status halted; coordinator exiting")
            return 2
        done = set(s.get("done", []))
        if len(done) >= 11:
            break
        w = s.get("cursor", 1)
        if w in done:
            w = min(set(range(1, 12)) - done)
        if not pipe_healthy():
            log(f"pipe unhealthy; sleeping {RETRY_SLEEP // 60} min before re-probe")
            time.sleep(RETRY_SLEEP)
            continue
        attempts = s.get("attempts", {})
        n = attempts.get(str(w), 0)
        # Auto-fix ladder: staging-only failure + intact workdir ->
        # cheap verify-first resume-stage; anything else -> full re-run.
        reason = wave_fail_reason(w)
        mode = "full"
        if n >= 1 and reason.startswith("staging:") and staging_resumable(w) and n < 3:
            mode = "resume-stage"
        log(f"pipe healthy; starting wave {w} (attempt {n + 1}, mode={mode})")
        ok = run_wave(w, mode)
        s = json.load(open(STATUS))
        if ok and w in s.get("done", []):
            attempts = s.get("attempts", {})
            attempts.pop(str(w), None)
            s["attempts"] = attempts
            s["updated_at"] = utcnow()
            json.dump(s, open(STATUS, "w"), indent=1)
            log(f"wave {w} PASS confirmed in status")
            continue
        n += 1
        attempts = s.get("attempts", {})
        attempts[str(w)] = n
        s["attempts"] = attempts
        reason = wave_fail_reason(w)
        log(f"wave {w} did not pass (attempt {n}/{MAX_ATTEMPTS}, reason: {reason})")
        if n >= MAX_ATTEMPTS:
            s["halted"] = True
            s["halt_reason"] = (f"wave {w} failed {n} consecutive times; "
                                f"last: {reason}")
            s["updated_at"] = utcnow()
            json.dump(s, open(STATUS, "w"), indent=1)
            log(f"HALT: {s['halt_reason']} -- needs operator review")
            return 1
        s["updated_at"] = utcnow()
        json.dump(s, open(STATUS, "w"), indent=1)
        log(f"auto-fix: sleeping {RETRY_SLEEP // 60} min before next attempt")
        time.sleep(RETRY_SLEEP)

    log("all 11 waves pass; launching merge")
    if not pipe_healthy():
        log("pipe unhealthy before merge; waiting")
        while not pipe_healthy():
            time.sleep(RETRY_SLEEP)
    p = subprocess.run([sys.executable, os.path.join(HERE, "vm_merge.py")],
                       capture_output=True, text=True, timeout=86400)
    log(f"merge exited rc={p.returncode}\ntail:\n" + "\n".join((p.stdout or "").splitlines()[-15:]))
    if p.returncode != 0:
        log(f"MERGE FAILED:\n{(p.stderr or '')[-2000:]}")
        return 1
    log("COORDINATOR COMPLETE: 11 waves + merge done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
