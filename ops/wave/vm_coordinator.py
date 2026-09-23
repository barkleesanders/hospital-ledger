#!/usr/bin/env python3
"""VM rebuild coordinator: pipe-aware sequential wave driver + merge.

- Probes R2 pipe health before each wave (single small GET, 90s budget).
- Runs vm_wave.py per wave as a foreground subprocess (collected, not fire-and-forget).
- On wave fail: back off 15 min, retry; 2 consecutive failures -> halt + alert.
- After wave 11 passes: runs vm_merge.py as a foreground subprocess.
- Heartbeat to vm-rebuild-progress.log every 15 min.
- Durable cursor in wave-orchestrator-status.json (owned by vm_wave.py).
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
PROBE_TIMEOUT = 90
RETRY_SLEEP = 900  # 15 min
HEARTBEAT_S = 900


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


def run_wave(w):
    env = dict(os.environ)
    # 8 workers: the egress proxy penalty-boxes CONNECT bursts; 8 concurrent
    # (the archive driver's proven level) with 3s stagger gaps stays safe.
    workers = "8"
    log(f"launching wave {w} ({workers} workers)")
    p = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "vm_wave.py"), str(w),
         "--workers", workers],
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


def main():
    os.makedirs(WAVES_DIR, exist_ok=True)
    log("coordinator start")
    consecutive_failures = 0
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
        log(f"pipe healthy; starting wave {w}")
        ok = run_wave(w)
        s = json.load(open(STATUS))
        if ok and w in s.get("done", []):
            consecutive_failures = 0
            log(f"wave {w} PASS confirmed in status")
            continue
        consecutive_failures += 1
        log(f"wave {w} did not pass (consecutive_failures={consecutive_failures})")
        if consecutive_failures >= 2:
            s["halted"] = True
            s["halt_reason"] = f"wave {w} failed twice"
            s["updated_at"] = utcnow()
            json.dump(s, open(STATUS, "w"), indent=1)
            log(f"HALT: wave {w} failed twice; needs operator review")
            return 1
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
