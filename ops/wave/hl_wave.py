#!/usr/bin/env python3
"""Hospital Ledger auto-research wave loop (v2).

Bounded research waves that keep hospital pricing data current, in the style
of the genealogy autoresearch waves: bounded per-wave work, evidence logs,
watermark advance, quiet on no-change, alert only when broken.

Wave mechanics (token-efficient by design):
  1. Standard loop-stop gate (~/workspace/tools/state/loops/hospital-ledger-wave.json).
  2. Take the next WAVE_SIZE seed MRF URLs whose host is in the Sentinel
     granted set. Deferred (previously changed, not yet reparsed) items go first.
  3. Change-detect with ONE conditional GET per URL (ETag/Last-Modified;
     sha256 compare). 304 = no change, no body. On 200 the body is streamed,
     hashed incrementally, and discarded — never held in RAM — with a hard
     750MB cutoff enforced mid-stream.
  4. First sighting of a URL: if its CCN is absent from prices/index.json,
     treat as changed (queue parse) — new hospitals must not wait for a change.
  5. Reparse changed MRFs (bounded by --max-reparse) with mrf_parse.py, then
     targeted slim (CCNS=..., SLIM_MERGE_INDEX=1) which writes
     public/data/prices/<ccn>.json and merges the index in place. Index
     completeness is verified after every slim run against a pre-wave snapshot;
     on regression the snapshot restores and the wave fails closed.
  6. Changed MRFs beyond the bound go to a durable deferred queue for the
     next wave — nothing is silently dropped.
  7. If any CCN updated: rebuild meta, assemble the contract tree
     (meta/*, prices/index.json, prices/<ccn>.json for updated CCNs),
     run counts-tier validation on meta/* PLUS pricing-tier validation on the
     new price files and the index (ALL must pass), publish to R2
     (prices/<ccn>.json + prices/index.json + meta/*, manifest LAST),
     byte-verify every uploaded key, then live-verify /api/manifest.
  8. Advance watermark, append evidence, reset failure streaks.

Safety (standing rules, updated 2026-09-23 per Barklee's end-to-end directive):
  - granted hosts only; no Sentinel policy/grant changes, ever
  - never publish unless ALL validation passes
  - R2 writes limited to meta/*, prices/index.json, and prices/<ccn>.json for
    updated CCNs; nothing is ever deleted from R2
  - every terminal failure routes through record_failure(); 3 consecutive
    failed waves -> standard loop-stop -> stop and alert

Usage:
  hl_wave.py [--wave-size 50] [--max-reparse 8] [--no-publish] [--dry-run]
             [--force-ccn CCN]
  --dry-run:    detect changes only, no parse/publish (proves mechanics)
  --no-publish: full pipeline incl. parse+validate, but no R2 upload
  --force-ccn:  force one granted-host CCN through the full reparse chain
                (exercises parse->slim->validate end to end on demand)
"""
import argparse, hashlib, importlib.util, json, os, shutil, socket, sqlite3, subprocess, sys, threading, time
import urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / "hospital-ledger"
GOAL = Path.home() / "workspace/goals/hospital-ledger-weekly-refresh-counts-tier/hidden_files"
VENV_PY = str(ROOT / ".venv/bin/python3")
WAVE_LOG = GOAL / "hl_waves.jsonl"
STATE_PATH = GOAL / "hl_wave_state.json"
QUEUE_PATH = GOAL / "hl_wave_queue.json"
STD_LOOP = Path.home() / "workspace/tools/state/loops/hospital-ledger-wave.json"
STOPCHECK = Path.home() / "workspace/tools/loop-stopcheck.sh"
INDEX_PATH = ROOT / "public/data/prices/index.json"
MAX_BYTES = 750 * 1024 * 1024
PRODUCER = "muse.ai nova (wave-loop)"
GOAL_SLUG = "hospital-ledger-weekly-refresh-counts-tier"
UA = {"User-Agent": "hospital-ledger-wave/2.0"}


def log(*a):
    print(f"[wave {datetime.now(timezone.utc).strftime('%H:%M:%S')}]", *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def strip_ipv6_no_proxy():
    # httpx 0.28.1 crashes on IPv6 literals in NO_PROXY (measured 2026-09-18)
    for k in ("NO_PROXY", "no_proxy"):
        v = os.environ.get(k)
        if v:
            os.environ[k] = ",".join(p for p in v.split(",") if ":" not in p)


# ---------------------------------------------------------------- standard loop gate

def load_std_loop():
    default = {"status": "active", "goal": GOAL_SLUG, "max_iter": 2000, "iter": 0,
               "predicates": {"target_met": False, "no_progress_streak": 0}}
    if STD_LOOP.exists():
        try:
            s = json.loads(STD_LOOP.read_text())
            for k, v in default.items():
                s.setdefault(k, v)
            s.setdefault("predicates", {}).update(
                {k: v for k, v in default["predicates"].items()
                 if k not in s.get("predicates", {})})
            return s
        except (OSError, json.JSONDecodeError):
            pass
    STD_LOOP.parent.mkdir(parents=True, exist_ok=True)
    STD_LOOP.write_text(json.dumps(default, indent=1))
    return dict(default)


def save_std_loop(s):
    STD_LOOP.write_text(json.dumps(s, indent=1))


def check_std_gate(s):
    """Inline mirror of loop-stopcheck.sh predicates. Returns stop reason or None."""
    if s.get("status") != "active":
        return "not-active"
    p = s.get("predicates", {}) or {}
    if p.get("target_met"):
        return "target-met"
    mx = s.get("max_iter", 0) or 0
    if mx and int(s.get("iter", 0) or 0) >= int(mx):
        return "max-iter"
    if int(p.get("no_progress_streak", 0) or 0) >= 3:
        return "no-progress"
    return None


# ---------------------------------------------------------------- failure routing

def record_failure(reason, code, std, extra=None):
    """Single choke point for every terminal failure."""
    rec = {"ts": now_iso(), "wave": "terminal", "result": "FAILED",
           "reason": reason}
    if extra:
        rec.update(extra)
    with open(WAVE_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    p = std.setdefault("predicates", {})
    p["no_progress_streak"] = int(p.get("no_progress_streak", 0) or 0) + 1
    std["iter"] = int(std.get("iter", 0) or 0) + 1
    save_std_loop(std)
    log(f"FAILURE: {reason} (streak={p['no_progress_streak']}) -> exit {code}")
    return code


def record_success(std):
    p = std.setdefault("predicates", {})
    p["no_progress_streak"] = 0
    std["iter"] = int(std.get("iter", 0) or 0) + 1
    save_std_loop(std)


# ---------------------------------------------------------------- queue / state

def load_granted():
    files = sorted(GOAL.glob("exact_hosts_granted_*.txt"))
    if not files:
        raise SystemExit("no granted-host inventory found")
    granted = {l.strip() for l in files[-1].read_text().splitlines() if l.strip()}
    log(f"granted set: {files[-1].name} -> {len(granted)} hosts")
    return granted


def build_queue(granted):
    db = sqlite3.connect(str(ROOT / "db/hospital_ledger.db"))
    rows = db.execute(
        "SELECT ccn, entity_name_common, mrf_url FROM mrf_seed WHERE mrf_url != ''"
    ).fetchall()
    db.close()
    q = []
    for ccn, name, url in rows:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host in granted:
            q.append({"ccn": ccn, "name": name or ccn, "url": url, "host": host})
    q.sort(key=lambda r: (r["ccn"], r["url"]))
    QUEUE_PATH.write_text(json.dumps({"built_at": now_iso(), "n": len(q), "queue": q}))
    log(f"queue built: {len(q)} granted-host MRF URLs")
    return q


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"pos": 0, "waves_done": 0, "urls": {}, "deferred": []}


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=1))


def load_index_ccns():
    """Set of CCNs present in the live price index. Empty set on any read failure."""
    try:
        d = json.loads(INDEX_PATH.read_text())
        return {h.get("ccn") for h in d.get("hospitals", []) if h.get("ccn")}, len(d.get("hospitals", []))
    except (OSError, json.JSONDecodeError) as e:
        log(f"WARNING: could not read index ({e}); treating index as empty")
        return set(), 0


# ---------------------------------------------------------------- change detection

def detect_changes(client, wave, state, index_ccns):
    """One conditional GET per URL. Returns (changed, errors, resolved, dead)."""
    changed, errors, dead = [], [], []
    resolved = 0
    for r in wave:
        url = r["url"]
        prev = state["urls"].get(url, {})
        if prev.get("dead"):
            resolved += 1
            continue
        hdrs = dict(UA)
        if prev.get("etag"):
            hdrs["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            hdrs["If-Modified-Since"] = prev["last_modified"]
        try:
            with client.stream("GET", url, headers=hdrs) as resp:
                sc = resp.status_code
                if sc == 304:
                    state["urls"][url] = {**prev, "probed_at": now_iso()}
                    resolved += 1
                    continue
                if sc in (404, 410):
                    state["urls"][url] = {**prev, "dead": True, "probed_at": now_iso()}
                    dead.append(r["ccn"])
                    resolved += 1
                    continue
                if sc != 200:
                    errors.append({"ccn": r["ccn"], "class": "http",
                                   "detail": f"status={sc}"})
                    continue
                # 200: stream, hash incrementally, enforce the byte cap mid-stream.
                h = hashlib.sha256()
                nbytes = 0
                too_large = False
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    nbytes += len(chunk)
                    if nbytes > MAX_BYTES:
                        too_large = True
                        break
                    h.update(chunk)
                if too_large:
                    errors.append({"ccn": r["ccn"], "class": "too_large",
                                   "detail": f">{MAX_BYTES} bytes"})
                    continue
                digest = h.hexdigest()
                meta = {"etag": resp.headers.get("etag"),
                        "last_modified": resp.headers.get("last-modified"),
                        "sha256": digest, "bytes": nbytes, "probed_at": now_iso()}
                if prev.get("sha256") and prev["sha256"] != digest:
                    log(f"CHANGED {r['ccn']} {url[:70]} ({prev.get('bytes', '?')} -> {nbytes} B)")
                    changed.append({**r, "sha256": digest, "bytes": nbytes})
                elif not prev.get("sha256"):
                    if r["ccn"] not in index_ccns:
                        # First sighting AND absent from the index: a new (or
                        # never-ingested) hospital — parse it now, don't wait.
                        log(f"NEW-TO-INDEX {r['ccn']} {nbytes} B -> queue parse")
                        changed.append({**r, "sha256": digest, "bytes": nbytes})
                    else:
                        log(f"baseline {r['ccn']} {nbytes} B")
                state["urls"][url] = meta
                resolved += 1
        except Exception as e:
            errors.append({"ccn": r["ccn"], "class": "transport",
                           "detail": f"{type(e).__name__}:{str(e)[:80]}"})
    return changed, errors, resolved, dead

# ---------------------------------------------------------------- reparse + slim

def reparse_changed(changed, max_reparse, errors):
    """Parse + targeted-slim each changed MRF. Returns (updated_ccns, deferred)."""
    updated, deferred = [], list(changed[max_reparse:])
    if deferred:
        log(f"{len(deferred)} changed MRFs deferred to next wave (bound={max_reparse})")
    for ch in changed[:max_reparse]:
        ccn, name, url = ch["ccn"], ch["name"], ch["url"]
        log(f"reparse {ccn} ...")
        p1 = subprocess.run([VENV_PY, "scripts/mrf_parse.py", "--ccn", ccn,
                             "--name", name, "--url", url],
                            cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
        if p1.returncode != 0:
            errors.append({"ccn": ccn, "class": "parse",
                           "detail": (p1.stderr or p1.stdout).strip()[-120:]})
            continue
        env2 = dict(os.environ)
        env2["CCNS"] = ccn
        env2["SLIM_MERGE_INDEX"] = "1"  # merge into existing index; never truncate
        p2 = subprocess.run([VENV_PY, "scripts/slim_parsed.py"],
                            cwd=str(ROOT), env=env2,
                            capture_output=True, text=True, timeout=1800)
        if p2.returncode != 0:
            errors.append({"ccn": ccn, "class": "slim",
                           "detail": p2.stderr.strip()[-120:]})
            continue
        price_path = ROOT / "public/data/prices" / f"{ccn}.json"
        if not price_path.exists() or price_path.stat().st_size == 0:
            errors.append({"ccn": ccn, "class": "slim",
                           "detail": f"{price_path.name} missing/empty after slim"})
            continue
        updated.append(ccn)
        log(f"reparse {ccn} OK")
    return updated, deferred


def verify_index_complete(pre_count, pre_ccns, updated_ccns):
    """The index must never shrink on a targeted wave. Returns (ok, count)."""
    ccns, count = load_index_ccns()
    new_ccns = {c for c in updated_ccns if c not in pre_ccns}
    expected = pre_count + len(new_ccns)
    missing_updated = [c for c in updated_ccns if c not in ccns]
    ok = (count == expected) and not missing_updated
    if not ok:
        log(f"INDEX REGRESSION: pre={pre_count} now={count} expected={expected} "
            f"missing_updated={missing_updated}")
    return ok, count


# ---------------------------------------------------------------- pricing-tier validation

def validate_pricing(out, updated_ccns, pre_count, pre_ccns):
    """Beyond the counts tier: every new price file + the index must be sound.

    Checks (all must pass):
      - out/prices/index.json parses, has >= pre_count hospitals (never fewer),
        and contains an entry for every updated CCN.
      - each out/prices/<ccn>.json parses, has items (non-empty list), every
        sampled item carries code+type, and its item count matches the index
        entry's n for that CCN.
    Returns (ok, message).
    """
    try:
        idx = json.loads((out / "prices/index.json").read_text())
        hospitals = idx.get("hospitals")
        if not isinstance(hospitals, list) or len(hospitals) < pre_count:
            return False, f"index hospitals={len(hospitals) if isinstance(hospitals, list) else '?'} < pre-wave {pre_count}"
        by_ccn = {h.get("ccn"): h for h in hospitals if h.get("ccn")}
        for ccn in updated_ccns:
            if ccn not in by_ccn:
                return False, f"index missing updated CCN {ccn}"
    except (OSError, json.JSONDecodeError) as e:
        return False, f"index unreadable: {e}"

    for ccn in updated_ccns:
        p = out / "prices" / f"{ccn}.json"
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return False, f"{ccn}.json unreadable: {e}"
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return False, f"{ccn}.json has no items"
        for it in items[:50]:
            if not it.get("code") or not it.get("type"):
                return False, f"{ccn}.json item missing code/type"
        entry_n = (by_ccn[ccn] or {}).get("n")
        if entry_n is not None and entry_n != len(items):
            return False, (f"{ccn}: index n={entry_n} != price-file items={len(items)}")
    return True, f"pricing OK: {len(updated_ccns)} CCNs, index {len(hospitals)} hospitals"


# ---------------------------------------------------------------- publish

def load_r2_env():
    envf = {}
    for line in Path.home().joinpath("hospital-ledger.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, val = line.partition("=")
            envf[k.strip()] = val.strip()
    return envf


def r2_get_sha256(key, penv):
    p = subprocess.run([VENV_PY, "scripts/r2_put.py", "--get-sha256", key],
                       cwd=str(ROOT), env=penv, capture_output=True, text=True, timeout=300)
    return p.stdout.strip() if p.returncode == 0 else None


LOCK_KEY = "_pipeline/publish.lock"
LOCK_TTL = 900               # lock document ttl, seconds
LOCK_HEARTBEAT = 300         # heartbeat refresh interval while publishing


class PublishLock:
    """Mutual exclusion for the live R2 keyspace around publish().

    Acquire = conditional PUT with If-None-Match: * (fails if a lock exists)
    containing {"owner","ts","ttl"}. A background heartbeat thread refreshes
    the lock every LOCK_HEARTBEAT seconds while publishing; release = DELETE
    on completion or failure (finally). A stale lock (ts+ttl in the past) may
    be broken by the acquirer with a logged warning (handled inside r2_put).

    No two publishers (daily wave, weekly refresh, rebuild waves) may hold
    the lock at once; a failed acquire fails the publish closed.
    """

    def __init__(self, penv, owner=None, ttl=LOCK_TTL,
                 heartbeat_interval=LOCK_HEARTBEAT, lock_key=LOCK_KEY):
        self.penv = penv
        self.owner = owner or f"hl_wave:{socket.gethostname()}:{os.getpid()}"
        self.ttl = ttl
        self.heartbeat_interval = heartbeat_interval
        self.lock_key = lock_key
        self.acquired = False
        self._stop = threading.Event()
        self._thread = None

    def _r2(self, *args):
        return subprocess.run(
            [VENV_PY, "scripts/r2_put.py", *args,
             "--ttl", str(self.ttl), "--lock-key", self.lock_key],
            cwd=str(ROOT), env=self.penv,
            capture_output=True, text=True, timeout=120)

    def acquire(self):
        r = self._r2("--lock-acquire", self.owner)
        if r.returncode != 0:
            log(f"publish lock NOT acquired: {(r.stdout + r.stderr).strip()[-200:]}")
            return False
        self.acquired = True
        self._thread = threading.Thread(target=self._heartbeat, daemon=True,
                                        name="publish-lock-heartbeat")
        self._thread.start()
        log(f"publish lock acquired: {self.lock_key} owner={self.owner}")
        return True

    def _heartbeat(self):
        while not self._stop.wait(self.heartbeat_interval):
            r = self._r2("--lock-refresh", self.owner)
            if r.returncode != 0:
                log(f"WARNING: publish-lock heartbeat failed: "
                    f"{(r.stdout + r.stderr).strip()[-160:]}")

    def release(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=30)
            self._thread = None
        if self.acquired:
            self.acquired = False
            r = self._r2("--lock-release", self.owner)
            if r.returncode == 0:
                log(f"publish lock released: {self.lock_key}")
            else:
                log(f"WARNING: publish-lock release failed: "
                    f"{(r.stdout + r.stderr).strip()[-160:]}")

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("publish lock held by another publisher")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def publish(out, updated_ccns, penv, lock_owner=None, lock_ttl=LOCK_TTL,
            lock_heartbeat_interval=LOCK_HEARTBEAT, lock_key=LOCK_KEY):
    """Upload per-CCN price files + index + meta (manifest LAST). Byte-verify each.

    The whole upload runs under _pipeline/publish.lock: acquire (conditional
    PUT), heartbeat while publishing, release (DELETE) on completion/failure.
    Returns (ok, message); published_ccns semantics: on ok=True the CCNs are
    exactly those byte-verified in R2 — no more, no fewer.
    """
    lock = PublishLock(penv, owner=lock_owner, ttl=lock_ttl,
                       heartbeat_interval=lock_heartbeat_interval, lock_key=lock_key)
    try:
        if not lock.acquire():
            return False, "publish lock held by another publisher"
        return _publish_locked(out, updated_ccns, penv)
    finally:
        lock.release()


def _publish_locked(out, updated_ccns, penv):
    keys = [f"prices/{ccn}.json" for ccn in updated_ccns]
    keys += ["prices/index.json", "meta/summary.json", "meta/hospitals.json",
             "meta/manifest.json"]
    pairs = []
    for k in keys:
        local = out / k
        pairs += [str(local), k]
    r = subprocess.run([VENV_PY, "scripts/r2_put.py"] + pairs,
                       cwd=str(ROOT), env=penv, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        return False, f"r2_put exit {r.returncode}: {(r.stdout + r.stderr)[-300:]}"
    for k in keys:
        want = hashlib.sha256((out / k).read_bytes()).hexdigest()
        got = r2_get_sha256(k, penv)
        if got != want:
            return False, f"byte-verify mismatch on {k}"
        log(f"verified {k}")
    return True, f"{len(keys)} keys uploaded + verified"


def live_verify_manifest(want_generated_at):
    for i in range(20):
        try:
            d = json.load(urllib.request.urlopen(
                f"https://hospitalledger.com/api/manifest?cb={int(time.time())}{i}", timeout=20))
            if d.get("generated_at") == want_generated_at and d.get("summary_source") == "r2":
                return True
        except Exception:
            pass
        time.sleep(10)
    return False

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave-size", type=int, default=50)
    ap.add_argument("--max-reparse", type=int, default=8)
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-ccn", default=None,
                    help="force one granted-host CCN through the full reparse chain")
    args = ap.parse_args()

    strip_ipv6_no_proxy()
    std = load_std_loop()
    reason = check_std_gate(std)
    if reason:
        print(f"LOOP STOPPED by standard gate ({reason}). Manual review required.",
              file=sys.stderr)
        return 3

    granted = load_granted()
    import httpx  # fail fast here: before the snapshot write below, so an
                  # import/startup failure exits before any state mutation
    queue = json.loads(QUEUE_PATH.read_text())["queue"] if QUEUE_PATH.exists() else build_queue(granted)
    if not queue or not all((urllib.parse.urlparse(r["url"]).hostname or "").lower() in granted
                            for r in queue[:50]):
        queue = build_queue(granted)

    state = load_state()
    n = len(queue)
    pos = state["pos"] % n if n else 0

    # Snapshot the index before any mutation (regression restore point).
    index_ccns, pre_count = load_index_ccns()
    snapshot = GOAL / f"index_snapshot_wave{state['waves_done'] + 1}.json"
    try:
        shutil.copy2(INDEX_PATH, snapshot)
    except OSError as e:
        return record_failure(f"index snapshot failed: {e}", 2, std)

    # Deferred (already-known-changed) items go first.
    deferred = state.get("deferred", []) or []
    wave_urls = [queue[(pos + i) % n] for i in range(min(args.wave_size, n))]
    log(f"wave {state['waves_done'] + 1}: {len(wave_urls)} URLs from pos {pos}/{n}, "
        f"{len(deferred)} deferred carried in")

    forced = None
    if args.force_ccn:
        hit = next((r for r in queue if r["ccn"] == args.force_ccn), None)
        if not hit:
            return record_failure(f"--force-ccn {args.force_ccn} not in granted queue", 2, std)
        forced = {**hit, "forced": True}
        log(f"forced reparse: {args.force_ccn}")

    changed, errors, resolved = [], [], 0
    dead = []
    with httpx.Client(timeout=30, follow_redirects=True, headers=UA) as c:
        changed, errors, resolved, dead = detect_changes(c, wave_urls, state, index_ccns)
    log(f"checked={len(wave_urls)} resolved={resolved} changed={len(changed)} "
        f"errors={len(errors)} dead={len(dead)}")

    # Pervasive probe failure: do not advance the watermark; fail the wave.
    if wave_urls and resolved / len(wave_urls) < 0.5:
        save_state(state)  # persist per-URL outcomes, but keep pos
        return record_failure("pervasive-probe-failure", 2, std,
                              {"checked": len(wave_urls), "resolved": resolved,
                               "errors": [e["detail"] for e in errors[:5]]})

    # Reparse: deferred first, then newly changed, then forced (forced always runs).
    to_parse = list(deferred) + changed
    if forced and forced["ccn"] not in {d.get("ccn") for d in to_parse}:
        to_parse.append(forced)
    updated_ccns, still_deferred = [], []
    if to_parse and not args.dry_run:
        updated_ccns, still_deferred = reparse_changed(to_parse, args.max_reparse, errors)
        ok, count = verify_index_complete(pre_count, index_ccns, updated_ccns)
        if not ok:
            try:
                shutil.copy2(snapshot, INDEX_PATH)
                log("index restored from pre-wave snapshot")
            except OSError as e:
                log(f"snapshot restore FAILED: {e}")
            return record_failure("index-regression", 2, std,
                                  {"updated_ccns": updated_ccns, "pre_count": pre_count})
    elif to_parse and args.dry_run:
        still_deferred = to_parse  # dry run: nothing parsed, nothing consumed
        log(f"dry run: {len(to_parse)} would-be reparses skipped")

    published = False
    validation = "skipped"
    pricing_validation = "skipped"
    # CCNs parsed in an earlier no-publish run still need their price files
    # uploaded whenever we next publish; otherwise the published index would
    # reference files that never reached R2.
    unpublished = [c for c in state.get("unpublished", []) or []
                   if (ROOT / "public/data/prices" / f"{c}.json").exists()]
    # Persist the pruned carry list even when this wave publishes nothing:
    # a CCN whose price file vanished must not linger in state forever.
    if unpublished != (state.get("unpublished") or []):
        state["unpublished"] = unpublished
        save_state(state)
    publish_ccns = []
    if (updated_ccns or unpublished) and not args.dry_run:
        env = dict(os.environ)
        if updated_ccns:
            env2 = dict(env)
            env2["CCNS"] = ",".join(updated_ccns)
            r1 = subprocess.run([VENV_PY, "scripts/patch_index_from_prices.py"],
                                cwd=str(ROOT), env=env2, capture_output=True, text=True, timeout=300)
            log(f"patch_index rc={r1.returncode}")
            if r1.returncode != 0:
                return record_failure("site-data-build-fail", 2, std,
                                      {"patch_rc": r1.returncode,
                                       "detail": r1.stderr[-600:]})
        else:
            log("carried-only publish: index already patched in earlier wave, skipping patch_index")
        r2 = subprocess.run([VENV_PY, "scripts/build_site_data.py"],
                            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
        log(f"build_site_data rc={r2.returncode}")
        if r2.returncode != 0:
            return record_failure("site-data-build-fail", 2, std,
                                  {"build_rc": r2.returncode,
                                   "detail": r2.stderr[-600:]})

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        out = ROOT / "data" / f"wave-out-{ts}"
        (out / "meta").mkdir(parents=True)
        (out / "prices").mkdir(parents=True)
        for src, dst in [("public/data/summary.json", "meta/summary.json"),
                         ("public/data/hospitals.json", "meta/hospitals.json"),
                         ("public/data/prices/index.json", "prices/index.json")]:
            (out / dst).write_bytes((ROOT / src).read_bytes())
        publish_ccns = list(dict.fromkeys(updated_ccns + unpublished))
        for ccn in publish_ccns:
            (out / "prices" / f"{ccn}.json").write_bytes(
                (ROOT / "public/data/prices" / f"{ccn}.json").read_bytes())
        subprocess.run([VENV_PY, "scripts/make_manifest.py", str(out),
                        "--producer", PRODUCER, "--tier", "counts",
                        "--note", f"wave {state['waves_done'] + 1} pricing updated_ccns={','.join(publish_ccns)}"],
                       cwd=str(ROOT), check=True, capture_output=True, text=True, timeout=120)

        v = subprocess.run([VENV_PY, "scripts/validate_site_data.py", str(out),
                            "--tier", "counts", "--quiet"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        validation = "PASS" if v.returncode == 0 else "FAIL"
        log(f"counts-tier validation: {validation}")
        if v.returncode != 0:
            return record_failure("counts-validation-fail", 2, std,
                                  {"updated_ccns": updated_ccns,
                                   "stderr": v.stderr[-500:]})

        ok, msg = validate_pricing(out, publish_ccns, pre_count, index_ccns)
        pricing_validation = "PASS" if ok else "FAIL"
        log(f"pricing-tier validation: {pricing_validation} ({msg})")
        if not ok:
            return record_failure("pricing-validation-fail", 2, std,
                                  {"updated_ccns": publish_ccns, "detail": msg})

        if not args.no_publish:
            penv = dict(env)
            penv.update(load_r2_env())
            penv["R2_BUCKET"] = "hl-mrf-parsed"
            chk = subprocess.run([VENV_PY, "scripts/r2_put.py", "--check"],
                                 cwd=str(ROOT), env=penv,
                                 capture_output=True, text=True, timeout=60)
            if chk.returncode != 0:
                return record_failure("r2-credential-rejected", 3, std)
            ok, msg = publish(out, publish_ccns, penv)
            log(f"publish: {msg}")
            if not ok:
                return record_failure("publish-fail", 3, std, {"detail": msg})
            want = json.loads((out / "meta/summary.json").read_text())["generated_at"]
            published = live_verify_manifest(want)
            log(f"live verify: {'OK' if published else 'FAILED'}")
            if not published:
                return record_failure("live-verify-fail", 4, std)
            state["unpublished"] = []
        else:
            log("--no-publish: assembled + validated, not uploaded")
            state["unpublished"] = list(dict.fromkeys(unpublished + updated_ccns))

    # Watermark advance (only reached on non-terminal waves).
    state["pos"] = (pos + len(wave_urls)) % n
    state["waves_done"] += 1
    state["deferred"] = still_deferred
    save_state(state)
    record_success(std)
    try:
        snapshot.unlink()
    except OSError:
        pass
    rec = {"ts": now_iso(), "wave": state["waves_done"],
           "checked": len(wave_urls), "resolved": resolved,
           "changed": len(changed), "dead": dead,
           "updated_ccns": updated_ccns,
           "published_ccns": publish_ccns if published else [],
           "deferred_pending": len(still_deferred),
           "errors": errors[:10],
           "validation": validation, "pricing_validation": pricing_validation,
           "published": published, "dry_run": args.dry_run,
           "no_publish": args.no_publish}
    with open(WAVE_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    log(f"wave {state['waves_done']} done: updated={updated_ccns} "
        f"published={published} errors={len(errors)} deferred={len(still_deferred)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
