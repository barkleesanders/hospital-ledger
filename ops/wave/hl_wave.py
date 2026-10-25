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
     750MB cutoff enforced mid-stream. If the direct leg raises a transport
     error (never on HTTP error statuses), the identical conditional GET is
     retried once through the keyed CORS proxy and the result is tagged
     VIA-PROXY in the wave log; if the proxy leg also fails, the transport
     error is recorded with which leg(s) failed.
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
import argparse, hashlib, importlib.util, json, os, re, shutil, socket, sqlite3, subprocess, sys, threading, time
import urllib.parse, urllib.request
import httpx  # fail fast at import: before any state mutation in main()
import threading
from concurrent.futures import ThreadPoolExecutor
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
# Self-recovering publish infrastructure (2026-10-01, Barklee directive: VM-first,
# Mini as little as possible, pricing updates must never be dropped).
PENDING_PATH = GOAL / "hl_pending_publish.json"          # publish backlog manifest
PUB_HASH_PATH = ROOT / "data" / "published_price_hashes.json"  # {ccn: sha256} at last publish
ROUTE_HEALTH_PATH = GOAL / "hl_r2_route_health.json"     # auto-learned R2 route health
REPARSE_PENDING_SENTINEL = "REPARSE-PENDING"              # forces re-detect next wave
# Retry policy for the pending-publish loop (own cron, every 20 min).
# 2026-10-23 (Barklee directive: "Don't do a six hour back off. Immediately
# always try to fix it."): the old 20min-doubling-to-6h schedule turned a
# one-key publish crash into a 5-day pricing stall. The retry cron ticks
# every 20 min, so the cap is one tick: a fixable failure is retried on the
# next tick, never parked for hours. Escalation at RETRY_MAX_ATTEMPTS is a
# human-attention signal only -- retries continue while the loop owns
# recovery.
RETRY_BASE_SECS = 300         # 5 min, doubled per attempt
RETRY_MAX_DELAY_SECS = 1200   # 20 min cap = one retry-cron tick
RETRY_MAX_ATTEMPTS = 20       # escalation threshold (signal, not a stop)
# Route-health cooldowns: after N consecutive R2 failures the route is assumed
# down and attempts fail fast (no burned timeouts) until the cooldown expires.
ROUTE_COOLDOWN_SCHEDULE = {3: 1800, 4: 3600}  # consec fails -> cooldown secs
ROUTE_COOLDOWN_CAP = 7200                     # 2 h max
INDEX_PATH = ROOT / "public/data/prices/index.json"
MAX_BYTES = 750 * 1024 * 1024
PRODUCER = "muse.ai nova (wave-loop)"
GOAL_SLUG = "hospital-ledger-weekly-refresh-counts-tier"
UA = {"User-Agent": "hospital-ledger-wave/2.0"}

# CORS-proxy egress fallback (v2, 2026-09-29). Direct leg raises a transport
# error -> retry the identical conditional GET once through the proxy.
PROXY_BASE = "https://hospital-ledger-cors-proxy.barkleesanders.workers.dev"
CORS_PROXY_KEY = None  # loaded once at startup; None => direct-only, never crashes


def load_cors_proxy_key():
    """Parse HL_CORS_PROXY_KEY from ~/hospital-ledger.env. Absent/unreadable:
    proxy leg stays disabled. The key value is never logged."""
    global CORS_PROXY_KEY
    try:
        for line in (Path.home() / "hospital-ledger.env").read_text().splitlines():
            if line.startswith("HL_CORS_PROXY_KEY="):
                v = line.split("=", 1)[1].strip()
                if v:
                    CORS_PROXY_KEY = v
                break
    except OSError:
        pass


def _scrub(msg):
    """Redact the proxy key from any message before it reaches a log."""
    if CORS_PROXY_KEY and CORS_PROXY_KEY in msg:
        return msg.replace(CORS_PROXY_KEY, "[redacted]")
    return msg


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


def load_edge():
    """scripts/hl_edge.py: routes hospital fetches via hospitalledger.com/api/fetch.

    HL_FETCH_PROXY / HL_FETCH_TOKEN come from the environment or, failing that,
    ~/hospital-ledger.env; they are exported so mrf_parse.py subprocesses inherit
    them. Unset = direct fetch, unchanged behaviour.
    (Ported from bundle ops/wave/hl_wave.py, pipeline sha 25b775fa, 2026-10-13;
    VM local evolution in this file preserved.)
    """
    envf = ROOT / "hospital-ledger.env"
    if not envf.exists():
        envf = Path.home() / "hospital-ledger.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip() in ("HL_FETCH_PROXY", "HL_FETCH_TOKEN") and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
    for d in (ROOT / "scripts", Path(__file__).resolve().parents[2] / "scripts"):
        if (d / "hl_edge.py").exists():
            spec = importlib.util.spec_from_file_location("hl_edge", d / "hl_edge.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    if os.environ.get("HL_FETCH_PROXY"):
        raise SystemExit("HL_FETCH_PROXY is set but scripts/hl_edge.py was not found")
    return None


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


# ---------------------------------------------------------------- self-recovering publish infrastructure
# (2026-10-01) Three mechanisms, all VM-local:
#  1. route health: consecutive R2 failures earn a cooldown; attempts during
#     cooldown fail fast instead of burning timeouts. Success resets.
#  2. pending manifest: any R2-side failure (sync or publish) parks the CCN set
#     in hl_pending_publish.json with exponential backoff. record_deferred logs
#     the terminal record WITHOUT bumping no_progress_streak -- a transport
#     outage is not research failure, and the retry loop owns recovery.
#  3. drift detection: {ccn: sha256} of every published price file is recorded;
#     any local file that drifts from its published hash is re-queued. Combined
#     with transactional URL state (consumed vs seen sha256), no wave can lose
#     a price update again.

def _now_ts():
    return time.time()


def load_route_health():
    try:
        h = json.loads(ROUTE_HEALTH_PATH.read_text())
        if isinstance(h, dict):
            return h
    except (OSError, ValueError):
        pass
    return {}


def save_route_health(h):
    tmp = str(ROUTE_HEALTH_PATH) + ".tmp"
    Path(tmp).write_text(json.dumps(h, indent=1))
    os.replace(tmp, ROUTE_HEALTH_PATH)


def _route_rec(h, op):
    rec = h.get(op)
    if not isinstance(rec, dict):
        rec = {"consec_fail": 0, "cooldown_until": 0,
               "last_ok": None, "last_fail": None}
        h[op] = rec
    return rec


def route_usable(h, op):
    """True unless the route is in a learned cooldown. Never raises."""
    rec = _route_rec(h, op)
    until = rec.get("cooldown_until", 0) or 0
    if until and _now_ts() < until:
        return False
    return True


def route_cooldown_left(h, op):
    rec = _route_rec(h, op)
    return max(0.0, (rec.get("cooldown_until", 0) or 0) - _now_ts())


def note_route(h, op, ok, detail=""):
    """Record one R2 attempt outcome; manage cooldowns. Returns the record."""
    rec = _route_rec(h, op)
    if ok:
        rec["consec_fail"] = 0
        rec["cooldown_until"] = 0
        rec["last_ok"] = now_iso()
    else:
        rec["consec_fail"] = rec.get("consec_fail", 0) + 1
        rec["last_fail"] = now_iso()
        n = rec["consec_fail"]
        if n in ROUTE_COOLDOWN_SCHEDULE:
            secs = ROUTE_COOLDOWN_SCHEDULE[n]
        elif n > max(ROUTE_COOLDOWN_SCHEDULE):
            secs = ROUTE_COOLDOWN_CAP
        else:
            secs = 0
        if secs:
            rec["cooldown_until"] = _now_ts() + secs
            log(f"route {op}: {n} consecutive failures -> cooldown {secs // 60}m "
                f"({detail})")
    save_route_health(h)
    return rec


def load_pending():
    try:
        p = json.loads(PENDING_PATH.read_text())
        if isinstance(p, dict) and p.get("publish_ccns"):
            return p
    except (OSError, ValueError):
        pass
    return None


def save_pending(p):
    tmp = str(PENDING_PATH) + ".tmp"
    Path(tmp).write_text(json.dumps(p, indent=1))
    os.replace(tmp, PENDING_PATH)


def clear_pending():
    try:
        PENDING_PATH.unlink()
    except OSError:
        pass


def record_deferred(reason, std, detail="", extra=None):
    """Log a transport-deferred terminal record WITHOUT counting a strike.

    A deferred publish is owned by the retry loop, not a wave failure: the
    research made progress (or had nothing to do), and no_progress_streak is
    left untouched. Returns 0 so the scheduler stays quiet -- the retry job
    alerts only if the backlog outlives RETRY_MAX_ATTEMPTS."""
    p = std.get("predicates", {}) or {}
    streak = int(p.get("no_progress_streak", 0) or 0)
    rec = {"ts": now_iso(), "wave": f"deferred-{int(std.get('iter', 0) or 0) + 1}",
           "result": "DEFERRED", "reason": reason, "detail": detail,
           "no_progress_streak": streak}
    if extra:
        rec.update(extra)
    with open(WAVE_LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    log(f"DEFERRED: {reason} ({streak} streak unchanged) -> "
        f"pending manifest, retry owns recovery")
    return 0


def load_published_hashes():
    try:
        d = json.loads(PUB_HASH_PATH.read_text())
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def save_published_hashes(d):
    PUB_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(PUB_HASH_PATH) + ".tmp"
    Path(tmp).write_text(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, PUB_HASH_PATH)


def find_drifted_ccns(published_hashes):
    """CCNs whose local price file differs from its last-published hash.

    Self-healing: any price update that reached local disk but never made it
    to R2 (crashed wave, killed publish, pre-baseline gap) is re-queued here.
    Files for CCNs with no baseline entry predate the scheme and are ignored.
    """
    drifted = []
    prices_dir = ROOT / "public/data/prices"
    if not prices_dir.is_dir():
        return drifted
    for path in prices_dir.glob("*.json"):
        ccn = path.stem
        base = published_hashes.get(ccn)
        if base is None or base == REPARSE_PENDING_SENTINEL:
            continue
        try:
            h = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if h != base:
            drifted.append(ccn)
    return sorted(drifted)


def find_missing_ccns(published_hashes):
    """CCNs with a published-hash baseline entry but no local price file.

    A vanished file is not re-detectable by change detection (the bytes were
    already seen), so without this the price update would be silently lost.
    Callers should requeue these CCNs for reparse.
    """
    prices_dir = ROOT / "public/data/prices"
    missing = []
    for ccn, base in published_hashes.items():
        if base is None or base == REPARSE_PENDING_SENTINEL:
            continue
        if not (prices_dir / f"{ccn}.json").exists():
            missing.append(ccn)
    return sorted(missing)


# ---------------------------------------------------------------- queue / state

def load_granted(edge=None):
    """Hosts this VM may contact. None = every seed host (edge proxy on)."""
    files = sorted(GOAL.glob("exact_hosts_granted_*.txt"))
    if edge is not None and edge.enabled():
        # Hosts already approved on this VM may still be fetched directly when
        # the edge cannot reach them (bot shield, bad origin cert); no others.
        if files:
            os.environ.setdefault("HL_EDGE_DIRECT_HOSTS_FILE", str(files[-1]))
        log("granted set: all seed hosts (fetched via the edge proxy; the VM contacts only its domain)")
        return None
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
        if granted is None or host in granted:
            q.append({"ccn": ccn, "name": name or ccn, "url": url, "host": host})
    q.sort(key=lambda r: (r["ccn"], r["url"]))
    mode = "all" if granted is None else "granted"
    QUEUE_PATH.write_text(json.dumps({"built_at": now_iso(), "n": len(q), "mode": mode, "queue": q}))
    log(f"queue built: {len(q)} {'seed' if granted is None else 'granted-host'} MRF URLs")
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

def detect_changes(client, wave, state, index_ccns, health=None):
    """Probe each URL in the wave (conditional GET); return (changed, errors,
    resolved, dead).

    `health` is the route-health dict (auto-learning, 2026-10-01): every
    fetch leg is gated by its learned cooldown and every attempt is recorded.
    """
    """One conditional GET per URL. Returns (changed, errors, resolved, dead).

    On a transport exception on the direct leg (never on HTTP error statuses),
    the identical conditional GET is retried once through the keyed CORS
    proxy. Proxy-served results are tagged VIA-PROXY in the wave log and
    counted; if the proxy leg also fails, the transport error names both legs.
    """
    changed, errors, dead = [], [], []
    resolved = 0
    proxy_fallbacks = 0
    state_lock = threading.Lock()  # protects state/changed/errors/dead/resolved/proxy_fallbacks
    health_lock = threading.Lock()  # protects route-health counters (2026-10-01)
# Parallel workers: I/O-bound fetches, 3 threads (pool set at the executor below)

    def proxy_url(u):
        return PROXY_BASE + "/?url=" + urllib.parse.quote(u, safe="")

    # Mini-via-SSH response adapter (2026-09-29): VM cannot reach the proxy
    # directly (Cloudflare 1010 IP block). The Mini fetches via the proxy
    # and returns the result over SSH multiplexing.
    class MiniResponse:
        def __init__(self, status, headers, body):
            self.status_code = status
            self.headers = headers
            self._body = body
        def iter_bytes(self, chunk_size=1 << 20):
            for i in range(0, len(self._body), chunk_size):
                yield self._body[i:i + chunk_size]

    def fetch_via_mini(url, via_proxy=True):
        """Fetch URL via Mini over SSH. Returns MiniResponse or None."""
        import subprocess, base64, json as js
        script = os.path.join(os.path.dirname(__file__), "mini_fetch.py")
        cmd = ["python3", script, url]
        if via_proxy:
            cmd.append("--via-proxy")
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
            data = js.loads(result.stdout)
            if data.get("error") or not data.get("status"):
                return None
            body = base64.b64decode(data["body_b64"]) if data["body_b64"] else b""
            return MiniResponse(data["status"], data["headers"], body)
        except Exception:
            return None

    def process_one(r):
        nonlocal resolved, proxy_fallbacks
        url = r["url"]
        with state_lock:
            prev = state["urls"].get(url, {})
        if prev.get("dead"):
            with state_lock:
                resolved += 1
            return
        hdrs = dict(UA)
        if prev.get("etag"):
            hdrs["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            hdrs["If-Modified-Since"] = prev["last_modified"]

        def handle(resp, via):
            """Process one response exactly as before; returns True if resolved."""
            nonlocal resolved, proxy_fallbacks
            with state_lock:  # thread-safe state updates (2026-09-29)
                return _handle_locked(resp, via)

        def _handle_locked(resp, via):
            nonlocal resolved, proxy_fallbacks
            sc = resp.status_code
            pfx = "VIA-PROXY " if via == "proxy" else ""
            if via == "proxy":
                proxy_fallbacks += 1
            if sc == 304:
                state["urls"][url] = {**prev, "probed_at": now_iso()}
                resolved += 1
                if pfx:
                    log(f"VIA-PROXY 304 {r['ccn']}")
                return True
            if sc in (404, 410):
                state["urls"][url] = {**prev, "dead": True, "probed_at": now_iso()}
                dead.append(r["ccn"])
                resolved += 1
                if pfx:
                    log(f"VIA-PROXY dead {r['ccn']} status={sc}")
                return True
            if sc != 200:
                errors.append({"ccn": r["ccn"], "class": "http",
                               "detail": f"status={sc} via={via}"})
                return False
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
                               "detail": f">{MAX_BYTES} bytes via={via}"})
                return False
            digest = h.hexdigest()
            # Transactional URL state (2026-10-01): "sha256" is the CONSUMED
            # hash -- bytes of the last successfully reparsed MRF -- and it
            # advances ONLY in reparse_changed on parse success, never here.
            # "seen_sha256" records the latest observed bytes. A wave that
            # detects changes but dies before reparse/publish therefore
            # re-detects the same CCNs next wave instead of losing them
            # (wave-23 class: 11 CCNs were consumed-but-never-parsed).
            meta = {**prev,
                    "etag": resp.headers.get("etag"),
                    "last_modified": resp.headers.get("last-modified"),
                    "seen_sha256": digest, "bytes": nbytes,
                    "probed_at": now_iso()}
            if prev.get("sha256") and prev["sha256"] != digest:
                log(f"{pfx}CHANGED {r['ccn']} {url[:70]} ({prev.get('bytes', '?')} -> {nbytes} B)")
                changed.append({**r, "sha256": digest, "bytes": nbytes})
            elif not prev.get("sha256"):
                if r["ccn"] not in index_ccns:
                    # First sighting AND absent from the index: a new (or
                    # never-ingested) hospital — parse it now, don't wait.
                    log(f"{pfx}NEW-TO-INDEX {r['ccn']} {nbytes} B -> queue parse")
                    changed.append({**r, "sha256": digest, "bytes": nbytes})
                else:
                    log(f"{pfx}baseline {r['ccn']} {nbytes} B")
            state["urls"][url] = meta
            resolved += 1
            return True

        # Fetch legs, cheapest first, each gated by the auto-learned route
        # health (2026-10-01): a leg in a learned cooldown is skipped fast
        # instead of burning its full timeout on every URL. The Mini leg is a
        # genuine last resort -- bounded, read-only, never required.
        legs = [leg for leg in ("direct", "proxy", "mini")
                if health is None or route_usable(health, f"mrf_{leg}")]
        if "proxy" in legs and not CORS_PROXY_KEY:
            legs.remove("proxy")
        if not legs:
            with state_lock:
                errors.append({"ccn": r["ccn"], "class": "transport",
                               "detail": "all fetch legs in learned cooldown"})
            return

        detail_parts = []
        for leg in legs:
            try:
                if leg == "direct":
                    with client.stream("GET", url, headers=hdrs) as resp:
                        handle(resp, "direct")
                elif leg == "proxy":
                    phdrs = dict(hdrs)
                    phdrs["x-cors-key"] = CORS_PROXY_KEY
                    log(f"VIA-PROXY retry {r['ccn']}")
                    with client.stream("GET", proxy_url(url), headers=phdrs) as resp:
                        handle(resp, "proxy")
                else:
                    log(f"VIA-MINI retry {r['ccn']}")
                    mini_resp = fetch_via_mini(url, via_proxy=True)
                    if mini_resp is None:
                        raise RuntimeError("mini fetch returned None")
                    handle(mini_resp, "mini-proxy")
                    with state_lock:
                        proxy_fallbacks += 1
                # A leg that produced ANY response (even an HTTP error status
                # recorded by handle()) is alive: HTTP errors never retry.
                if health is not None:
                    with health_lock:
                        note_route(health, f"mrf_{leg}", True)
                return
            except (httpx.TransportError, httpx.TimeoutException) as e:
                detail_parts.append(f"{leg}:{type(e).__name__}")
                if health is not None:
                    with health_lock:
                        note_route(health, f"mrf_{leg}", False, type(e).__name__)
            except Exception as e:
                detail_parts.append(f"{leg}:{type(e).__name__}")
                if health is not None:
                    with health_lock:
                        note_route(health, f"mrf_{leg}", False,
                                   _scrub(str(e))[:80])
        with state_lock:
            errors.append({"ccn": r["ccn"], "class": "transport",
                           "detail": " ".join(detail_parts)[:160]})

# PARALLEL EXECUTION: 3 workers for I/O-bound fetches (see executor below)
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(process_one, wave))

    if proxy_fallbacks:
        log(f"proxy fallbacks served: {proxy_fallbacks}")
    return changed, errors, resolved, dead

# ---------------------------------------------------------------- reparse + slim

def reparse_changed(changed, max_reparse, errors, state):
    """Parse + targeted-slim each changed MRF. Returns (updated_ccns, deferred).

    On a successful parse the URL's CONSUMED sha256 advances to the parsed
    bytes (transactional URL state, 2026-10-01). A failed parse leaves the
    consumed hash untouched, so the CCN is re-detected next wave."""
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
        # Consume: these MRF bytes are now reflected in the local price file.
        url = ch.get("url")
        if url and isinstance(state.get("urls", {}).get(url), dict):
            state["urls"][url]["sha256"] = (
                ch.get("sha256") or state["urls"][url].get("seen_sha256"))
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
    # Caller timeout must EXCEED r2_put.py's internal retry budget (300s
    # here): a caller cap <= the callee budget kills the callee mid-resume
    # on a stalled proxy (2026-10-23: a 2MB GET reached ~87% in 300s and
    # was SIGKILLed by the caller's own timeout, not by any network fault).
    p = subprocess.run([VENV_PY, "scripts/r2_put.py", "--get-sha256", key],
                       cwd=str(ROOT), env=penv, capture_output=True, text=True, timeout=360)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    # S3 read path flapping (2026-10-01): verify against the CDN-served copy.
    # Fail-closed: a stale CDN copy yields a mismatch, never a false match.
    tmp = f"/tmp/hl-verify-{key.replace('/', '_')}"
    if _cdn_get(key, tmp):
        digest = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log(f"byte-verify {key} via CDN fallback")
        return digest
    return None


def r2_get_file_fallback(key, dest, renv, health=None):
    """GET key -> dest. VM-ONLY (2026-10-01): r2_put.py now self-heals
    truncated VM-egress bodies via ranged requests, so the Mini leg that
    2026-09-29 added is gone. The route-health gate fails fast during a
    learned cooldown instead of burning 300s per key on a dead route.
    Returns (ok: bool, via: str)."""
    dest = str(dest)
    s3_note = ""
    if health is not None and not route_usable(health, "r2_get"):
        left = route_cooldown_left(health, "r2_get")
        s3_note = f"s3-cooldown-{left / 60:.0f}m"
    else:
        try:
            p = subprocess.run([VENV_PY, "scripts/r2_put.py", "--get-file", key, dest],
                               cwd=str(ROOT), env=renv, capture_output=True, text=True,
                               timeout=660)  # > r2_put get_file's internal 600s resume budget (2026-10-23)
        except subprocess.TimeoutExpired:
            if health is not None:
                note_route(health, "r2_get", False, "get-file timeout")
            s3_note = "s3-timeout"
        else:
            ok = p.returncode == 0
            if health is not None:
                note_route(health, "r2_get", ok,
                           p.stderr.strip()[-120:] if not ok else "")
            if ok:
                return True, "vm-direct"
            s3_note = f"s3-failed({p.stderr.strip()[-60:]})"
        log(f"R2 GET {key} via VM failed ({s3_note})")
    # CDN fallback carries its own health key: an S3 cooldown must not block it.
    if health is not None and not route_usable(health, "cdn_get"):
        left = route_cooldown_left(health, "cdn_get")
        return False, f"{s3_note};cdn-cooldown-{left / 60:.0f}m"
    if _cdn_get(key, dest):
        if health is not None:
            note_route(health, "cdn_get", True, "")
        log(f"R2 GET {key} via CDN fallback OK")
        return True, "cdn"
    if health is not None:
        note_route(health, "cdn_get", False, "cdn get failed")
    return False, f"{s3_note};cdn-failed"


CDN_BASE = "https://hospitalledger.com/data"  # R2 key k served at /data/k


def _cdn_get(key, dest):
    """Read-only fallback: fetch R2 `key` via the site's public CDN.

    2026-10-01: the runtime egress proxy flaps hard on
    *.r2.cloudflarestorage.com (large transfers stall even though small
    ones pass), while the same proxy pulls 1.5 MB from hospitalledger.com
    in ~4s. The CDN serves the exact R2 object bytes; a cache-buster query
    forces a fresh origin read. Never used for writes. A stale CDN copy
    can only cause a fail-closed mismatch downstream, never a false OK.
    Returns True on success."""
    url = f"{CDN_BASE}/{key}?cb={int(time.time())}"
    tmp = f"{dest}.cdnpart"
    try:
        # curl, not urllib: through this proxy curl completes multi-MB
        # transfers that stall under urllib (observed 2026-10-01).
        pr = subprocess.run(
            ["curl", "-sS", "-m", "300", "-o", tmp, "-w", "%{http_code}",
             "-A", "hl-wave/1.0", url],
            capture_output=True, text=True, timeout=320)
        code = pr.stdout.strip()[-3:]
        if pr.returncode == 0 and code == "200" and os.path.getsize(tmp) > 0:
            os.replace(tmp, dest)
            return True
        log(f"CDN GET {key} failed (curl rc={pr.returncode} http={code} "
            f"err={pr.stderr.strip()[-80:]})")
    except Exception as e:  # noqa: BLE001 - transport fallback, log and move on
        log(f"CDN GET {key} failed ({type(e).__name__})")
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return False


def _s3_get_to(key, tmp, renv):
    """One-shot direct S3 GET of `key` to `tmp` (no fallback legs, no route-
    health bookkeeping -- a bounded diagnostic read). True on success."""
    try:
        p = subprocess.run([VENV_PY, "scripts/r2_put.py", "--get-file", key, str(tmp)],
                           cwd=str(ROOT), env=renv, capture_output=True, text=True,
                           timeout=660)  # > r2_put get_file's internal 600s resume budget (2026-10-23)
        return p.returncode == 0 and Path(tmp).exists() and Path(tmp).stat().st_size > 0
    except Exception:  # noqa: BLE001 - diagnostic leg, any failure = no confirm
        return False


def _base_sanity(key, dest):
    """Cheap structural sanity for a synced build base. Returns (ok, detail).

    For prices/index.json this is the same floor the wave's corrupt-base
    guard uses elsewhere: valid JSON with >= 3000 hospitals. It cannot prove
    freshness -- that is the dual-leg check's job -- it only proves the base
    is a structurally real index, not a placeholder or an error body."""
    try:
        data = Path(dest).read_bytes()
    except OSError as e:
        return False, f"base unreadable: {e}"
    if not data:
        return False, "base is empty"
    if key == "prices/index.json":
        try:
            idx = json.loads(data)
            n = len(idx.get("hospitals") or [])
        except ValueError:
            return False, "base index is not valid JSON"
        if n < 3000:
            return False, f"base index has only {n} hospitals (<3000 floor)"
        return True, f"{n} hospitals"
    return True, f"{len(data)}B"


def verify_synced_base(key, dest, via=None, renv=None):
    """Verify a just-synced build base against the live manifest's artifact
    record for `key` (size + sha256). Returns (ok, detail).

    2026-10-13 structural fix: wave 35's S3 sync failed, the CDN fallback
    served the STALE site-bundled index (3,810 hospitals, committed Sept 27)
    instead of the canonical R2 object (3,823), and only the downstream
    count gate kept the regressed build from publishing. A build base is
    now verified at sync time against /api/manifest's own artifact record
    -- the same record live-verify trusts after publish. Fail-closed: an
    unreachable manifest, a missing artifact record, or any size/sha
    mismatch is NOT ok, and the caller parks (transport class) instead of
    building on an unverified base.

    2026-10-23 manifest-stale auto-heal (Barklee: auto learn / auto fix,
    never a long backoff): a mismatch has TWO possible causes and the old
    code could not tell them apart, so it deadlocked for 5 days. (a) The
    synced base is stale/corrupt (wave 35) -- block. (b) The R2 object
    ADVANCED past the manifest: the 2026-10-18 weekly counts publish PUT a
    new 3,842-hospital index + hospitals.json, then the egress proxy killed
    the meta/summary.json PUT before the manifest (uploaded last) landed,
    leaving the Oct-15 manifest describing an index R2 no longer holds.
    Discriminator for (b): fetch the same key through the OTHER independent
    leg (S3 direct if the sync came via CDN, CDN if it came via S3). If
    both legs return byte-identical content AND the base passes structural
    sanity, the base IS the canonical R2 object and the manifest record is
    the stale artifact -- proceed; this run's publish regenerates the
    manifest LAST and restores R2 consistency, and the downstream counts +
    pricing validation and post-publish live-verify gates still apply in
    full. If the legs disagree or cannot both be read, fail closed exactly
    as before."""
    try:
        text, _via = _fetch_live_manifest_text(
            f"https://hospitalledger.com/api/manifest?cb={int(time.time())}")
    except Exception as e:  # noqa: BLE001 - fail closed on any fetch error
        return False, f"live manifest fetch raised {type(e).__name__}"
    if not text:
        return False, "live manifest unreachable on all fetch legs"
    try:
        man = json.loads(text)
    except ValueError:
        return False, "live manifest is not valid JSON"
    rec = (man.get("artifacts") or {}).get(key)
    if not rec or not rec.get("sha256"):
        return False, f"live manifest has no artifact record for {key}"
    try:
        data = Path(dest).read_bytes()
    except OSError as e:
        return False, f"synced base unreadable: {e}"
    sha = hashlib.sha256(data).hexdigest()
    if len(data) == rec.get("bytes") and sha == rec.get("sha256"):
        return True, (f"{key} base verified vs live manifest "
                      f"({len(data)}B, sha {sha[:12]}...)")
    mismatch = (f"{key} base mismatch: synced {len(data)}B "
                f"sha={sha[:12]}..., manifest records "
                f"{rec.get('bytes')}B sha={str(rec.get('sha256'))[:12]}...")
    # --- manifest-stale discriminator: is the synced base the canonical
    # R2 object, confirmed byte-identical through a second independent leg?
    sane, sane_detail = _base_sanity(key, dest)
    if not sane:
        return False, f"{mismatch}; base also fails sanity ({sane_detail})"
    if renv is None:
        return False, mismatch
    tmp = Path(f"{dest}.altleg")
    try:
        if via == "vm-direct":
            alt_ok, alt_leg = _cdn_get(key, str(tmp)), "cdn"
        else:
            alt_ok, alt_leg = _s3_get_to(key, tmp, renv), "vm-direct"
        if alt_ok:
            alt_sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
            if alt_sha == sha:
                log(f"base verify: MANIFEST-STALE auto-heal for {key}: base "
                    f"confirmed byte-identical via {via} + {alt_leg} legs "
                    f"and sane ({sane_detail}); the live manifest's record "
                    f"is stale -- proceeding on the canonical R2 object; "
                    f"this publish regenerates the manifest last")
                return True, (f"{key} base accepted via manifest-stale "
                              f"auto-heal (dual-leg byte-identical, "
                              f"{sane_detail}); manifest record was "
                              f"{rec.get('bytes')}B sha={str(rec.get('sha256'))[:12]}...")
        return False, mismatch
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
        self.release_retry_delay = 5  # seconds between lock-release retries
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
        # VM-ONLY (2026-10-01): no Mini fallback. A failed acquire is either a
        # live lock (another publisher) or dead R2 transport; the caller
        # retries with backoff and then parks the CCNs for the retry loop.
        # 2026-10-13: the egress proxy also cuts the small lock ops
        # (RemoteDisconnected while reading the response), and a cut
        # response is ambiguous -- the conditional PUT may have landed.
        # Retry transport-looking failures a few times, and if R2 reports
        # the lock held by THIS owner, an earlier attempt of ours landed
        # it: adopt the lock instead of failing as "held by another".
        delays = (5, 15)
        for attempt in range(3):
            r = self._r2("--lock-acquire", self.owner)
            if r.returncode == 0:
                self.acquired = True
                break
            out = (r.stdout + r.stderr).strip()
            # Parse the HOLDER: r2_put ownership errors name both parties
            # ("lock held by <holder>", "... not <requester>"), so a plain
            # substring check for our owner false-matches the requester
            # mention -- that false-adopted a foreign zombie lock live on
            # 2026-10-14T01:03Z. Only the holder capture counts.
            m = re.search(r"lock held by (\S+)", out)
            holder = m.group(1).rstrip(".,") if m else None
            if holder is not None and holder == self.owner:
                log(f"publish lock adopted (own earlier acquire landed): "
                    f"{self.lock_key} owner={self.owner}")
                self.acquired = True
                break
            if "lock held" in out:
                log(f"publish lock NOT acquired via VM: {out[-160:]}")
                return False
            log(f"publish lock acquire transport failure (attempt "
                f"{attempt + 1}/3): {out[-160:]}")
            if attempt < 2:
                time.sleep(delays[attempt])
        if not self.acquired:
            return False
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
            # 2026-10-13: retry the release. One cut DELETE (RemoteDisconnected
            # through the egress proxy, wave of 2026-10-14T00:26Z) left a
            # zombie lock that stalled the publish for the full 900s TTL.
            # The release is owner-matched and idempotent inside r2_put,
            # so retrying is safe.
            for attempt in range(3):
                r = self._r2("--lock-release", self.owner)
                if r.returncode == 0:
                    log(f"publish lock released: {self.lock_key}")
                    return
                log(f"WARNING: publish-lock release failed (attempt "
                    f"{attempt + 1}/3): {(r.stdout + r.stderr).strip()[-160:]}")
                if attempt < 2:
                    time.sleep(self.release_retry_delay)

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
    # 2026-10-13: upload ONE key per r2_put invocation, retrying each key.
    # The old single invocation uploaded all 12 keys in one process; the
    # egress proxy kept cutting a connection mid-run (RemoteDisconnected),
    # which discarded every key's progress and burned the wave's publish
    # attempts inside one flap window. PUTs are idempotent (same bytes),
    # so per-key retry is safe; order is preserved (manifest LAST).
    for k in keys:
        local = out / k
        last_err = ""
        for attempt in range(3):
            r = subprocess.run([VENV_PY, "scripts/r2_put.py", str(local), k],
                               cwd=str(ROOT), env=penv, capture_output=True,
                               text=True, timeout=600)
            if r.returncode == 0:
                break
            last_err = (r.stdout + r.stderr)[-300:]
            log(f"publish upload {k} failed (attempt {attempt + 1}/3): "
                f"{last_err[-120:]}")
            if attempt < 2:
                time.sleep(20)
        else:
            # VM-ONLY (2026-10-01): no Mini fallback. The caller
            # (_publish_with_retry) retries with backoff; persistent failure
            # parks the CCNs in the pending manifest for the retry loop.
            return False, f"VM publish failed on {k}: {last_err}"
    for k in keys:
        want = hashlib.sha256((out / k).read_bytes()).hexdigest()
        got = r2_get_sha256(k, penv)
        if got != want:
            return False, f"byte-verify mismatch on {k}"
        log(f"verified {k}")
    return True, f"{len(keys)} keys uploaded + verified"


def _publish_with_retry(out, publish_ccns, penv, health, attempts=3):
    """Up to `attempts` VM publish runs with exponential backoff.

    The route-health gate fails fast during a learned cooldown instead of
    burning 30 minutes on a dead route. Returns (ok, msg)."""
    last = ""
    for i in range(attempts):
        if not route_usable(health, "r2_put"):
            left = route_cooldown_left(health, "r2_put")
            last = f"r2_put in learned cooldown ({left / 60:.0f}m left)"
            log(f"publish attempt {i + 1}: {last} -- parking for retry")
            break
        ok, msg = publish(out, publish_ccns, penv)
        # 2026-10-13: lock contention is not route health. A zombie lock
        # left by a cut release (or a live foreign publisher) says nothing
        # about the R2 data path; counting it as an r2_put failure earned
        # a bogus 30m cooldown that delayed a healthy retry by an hour.
        if not (not ok and msg.startswith("publish lock held")):
            note_route(health, "r2_put", ok, msg[-120:] if not ok else "")
        log(f"publish attempt {i + 1}/{attempts}: {msg}")
        if ok:
            return True, msg
        last = msg
        if i < attempts - 1:
            wait = 30 * (2 ** i)
            log(f"publish failed; retrying in {wait}s")
            time.sleep(wait)
    return False, last or "no publish attempt made"


def park_for_retry(publish_ccns, wave_no, reason, detail, attempts):
    """Park CCNs in the pending manifest with bounded backoff.

    `attempts` is the count of failed publish attempts so far; the next retry
    is due in min(RETRY_BASE_SECS * 2**attempts, RETRY_MAX_DELAY_SECS) --
    capped at one retry-cron tick (2026-10-23: no long backoffs; fix and
    retry immediately). Atomic write (tmp + replace)."""
    delay = min(RETRY_BASE_SECS * (2 ** max(0, attempts)), RETRY_MAX_DELAY_SECS)
    save_pending({
        "ts": now_iso(), "wave": wave_no,
        "publish_ccns": list(dict.fromkeys(publish_ccns)),
        "attempts": attempts, "next_retry_at": _now_ts() + delay,
        "reason": reason, "detail": (detail or "")[:300],
    })
    log(f"parked {len(publish_ccns)} CCNs for retry "
        f"(attempts={attempts}, next in {delay // 60}m): {reason}")


def _fetch_live_manifest_text(url):
    """Fetch the live manifest through VM-direct, the CORS proxy, then Mini.

    Leg order is cheapest-first; the Mini is a genuine last resort (read-only,
    bounded, never required). Returns (text, via) or (None, None)."""
    # leg 1: VM direct egress
    try:
        r = subprocess.run(["curl", "-s", "-m", "25", url],
                           capture_output=True, text=True, timeout=40)
        if r.returncode == 0 and r.stdout.strip().startswith("{"):
            return r.stdout, "direct"
    except Exception:
        pass
    # leg 2: keyed CORS proxy (VM -> Worker -> origin)
    if CORS_PROXY_KEY:
        try:
            purl = PROXY_BASE + "/?url=" + urllib.parse.quote(url, safe="")
            r = subprocess.run(
                ["curl", "-s", "-m", "30", "-H", f"x-cors-key: {CORS_PROXY_KEY}", purl],
                capture_output=True, text=True, timeout=45)
            if r.returncode == 0 and r.stdout.strip().startswith("{"):
                return r.stdout, "proxy"
        except Exception:
            pass
    # leg 3: Mini over SSH (last resort; 2026-09-29 the VM egress got
    # Cloudflare 1010 on this host)
    try:
        import shlex
        r = subprocess.run(
            ["ssh", "-F", "/home/hatch/.ssh/mini_ssh_config",
             "-o", "ConnectTimeout=15", "mini",
             f"curl -s -m 20 {shlex.quote(url)}"],
            capture_output=True, timeout=60, text=True)
        if r.returncode == 0 and r.stdout.strip().startswith("{"):
            return r.stdout, "mini"
    except Exception:
        pass
    return None, None


def live_verify_manifest(want_generated_at, publish_ccns=None, out_dir=None):
    # 2026-09-29: /api/manifest is now deployed on the Worker (serves R2 meta/manifest.json).
    # Strong verification: generated_at must match AND artifact hashes must match
    # the locally generated files. CCN presence alone is NOT sufficient.
    # 2026-10-01: fetch legs are VM-direct -> CORS proxy -> Mini (last resort).
    import hashlib
    for i in range(30):
        try:
            url = f"https://hospitalledger.com/api/manifest?cb={int(time.time())}{i}"
            text, via = _fetch_live_manifest_text(url)
            if text is None:
                log(f"live-verify attempt {i+1}: all fetch legs failed")
                time.sleep(10)
                continue
            m = json.loads(text)
            if m.get("generated_at") != want_generated_at:
                log(f"live-verify attempt {i+1} (via {via}): got {m.get('generated_at')}, want {want_generated_at}")
                time.sleep(10)
                continue
            # generated_at matches — now verify artifact hashes if out_dir given
            if out_dir:
                artifacts = m.get("artifacts", {})
                for key, info in artifacts.items():
                    want_hash = info.get("sha256")
                    if not want_hash:
                        continue
                    local_path = out_dir / key
                    if not local_path.exists():
                        log(f"live-verify: local {key} missing, skipping hash check")
                        continue
                    got_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
                    if got_hash != want_hash:
                        log(f"live-verify: HASH MISMATCH on {key}: want {want_hash[:12]}... got {got_hash[:12]}...")
                        return False
                    log(f"live-verify: {key} hash matches")
            log(f"live-verify: manifest generated_at={want_generated_at} confirmed")
            return True
        except Exception as e:
            log(f"live-verify attempt {i+1}: {e}")
        time.sleep(10)
    return False

# ---------------------------------------------------------------- git provenance
# The publish manifest carries git_commit/git_repo so the site banner can link
# the data to the exact GitHub commit. After a verified R2 publish, the manifest
# + publish record are committed to the repo (allowlisted paths only, never -A)
# and pushed to main.

MINI_SSH = ["ssh", "-F", "/home/hatch/.ssh/mini_ssh_config",
            "-o", "ConnectTimeout=20", "mini"]
MINI_REPO = "/Users/barkleesanders/projects/hospital-ledger"
GIT_REPO_URL = "https://github.com/barkleesanders/hospital-ledger"
# Allowlisted repo paths the wave may commit. NEVER use `git add -A`.
GIT_ALLOWLIST = ["data/publish-state.json"]


def mini_git_head():
    """HEAD SHA of the Mini repo (main). None on any failure (non-fatal)."""
    try:
        r = subprocess.run(
            MINI_SSH + ["git", "-C", MINI_REPO, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30)
        sha = r.stdout.strip()
        return sha if r.returncode == 0 and len(sha) == 40 else None
    except Exception as e:
        log(f"mini_git_head failed: {e}")
        return None


def git_provenance_commit(out_dir, wave_rec):
    """Commit the published manifest + wave record to the repo and push.

    Copies out/meta/manifest.json to the Mini repo's allowlisted
    data/publish-state.json (with the wave record attached), commits only the
    allowlisted paths, and pushes to main. Returns (ok, message); never raises.
    A failure here is a provenance-tracking gap, not a publish failure.
    """
    try:
        manifest = json.loads((out_dir / "meta/manifest.json").read_text())
        state_doc = {
            "manifest": manifest,
            "wave": wave_rec,
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }
        local_tmp = Path("/tmp/hl_publish_state.json")
        local_tmp.write_text(json.dumps(state_doc, indent=2, sort_keys=True) + "\n")

        # 1. copy to Mini repo
        r = subprocess.run(
            ["scp", "-F", "/home/hatch/.ssh/mini_ssh_config",
             "-o", "ConnectTimeout=20", str(local_tmp),
             f"mini:{MINI_REPO}/data/publish-state.json"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, f"scp failed: {r.stderr[-200:]}"

        # 2. commit allowlisted paths + push (single SSH invocation)
        cmd = (
            f"cd {MINI_REPO} && "
            f"git add {' '.join(GIT_ALLOWLIST)} && "
            f"git diff --cached --quiet || "
            f"git commit -m 'chore(data): wave publish {wave_rec.get('wave', '?')} "
            f"generated_at={manifest.get('generated_at', '?')}' && "
            f"git push origin main"
        )
        r = subprocess.run(MINI_SSH + [cmd],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False, f"commit/push failed: {(r.stdout + r.stderr)[-300:]}"
        return True, "provenance committed + pushed"
    except Exception as e:
        return False, f"exception: {e}"


# ---------------------------------------------------------------- publish phase (VM-only, self-recovering)

def retry_pending_publish(args, env, std):
    """--retry-pending entry point: drain the pending publish manifest.

    Exits 0 when nothing is due. A still-failing transport re-parks with
    escalated backoff (no strike -- the loop owns recovery). A
    logic/validation/credential failure records a strike so Barklee is
    alerted. Past RETRY_MAX_ATTEMPTS attempts an ESCALATION line is
    logged for the cron to surface.
    """
    pending = load_pending()
    if not pending or not pending.get("publish_ccns"):
        log("retry-pending: nothing pending")
        return 0
    now = _now_ts()
    if pending.get("next_retry_at", 0) > now:
        wait = int(pending["next_retry_at"] - now)
        log(f"retry-pending: not due yet ({wait // 60}m left)")
        return 0
    _vanished = [c for c in pending["publish_ccns"]
                 if not (ROOT / "public/data/prices" / f"{c}.json").exists()]
    if _vanished:
        # Not silent: a vanished file is re-detectable only via the drift
        # net's reparse requeue (change detection can't see it). Loud log so
        # the gap is visible until the drift net requeues it next wave.
        log(f"WARNING: {len(_vanished)} pending CCNs' price files vanished: {_vanished}")
    ccns = [c for c in pending["publish_ccns"]
            if (ROOT / "public/data/prices" / f"{c}.json").exists()]
    if not ccns:
        log("retry-pending: every pending CCN price file vanished; clearing manifest")
        clear_pending()
        return 0
    attempts = int(pending.get("attempts", 0))
    if attempts >= RETRY_MAX_ATTEMPTS:
        log(f"ESCALATION: publish retry attempts={attempts} for {len(ccns)} CCNs "
            f"(parked since {pending.get('ts')}); transport still failing")
    log(f"retry-pending: attempt {attempts + 1} for {len(ccns)} CCNs")
    # Bump the attempt count BEFORE the run so a deferral re-parks with
    # escalated backoff (run_publish_phase carries the file's count).
    pending["attempts"] = attempts + 1
    save_pending(pending)
    state = load_state()
    index_ccns, pre_count = load_index_ccns()
    health = load_route_health()
    retry_args = argparse.Namespace(dry_run=False, no_publish=False)
    pub = run_publish_phase(retry_args, env, state, std, ccns, [],
                            index_ccns, pre_count, [], health,
                            state.get("waves_done", 0) + 1)
    save_route_health(health)
    status = pub.get("status")
    if status == "published":
        log(f"retry-pending: PUBLISHED {pub['publish_ccns']} "
            f"after {attempts + 1} attempt(s)")
        record_success(std)
        return 0
    if status == "deferred":
        p = load_pending() or {}
        delay = min(RETRY_BASE_SECS * (2 ** (attempts + 1)), RETRY_MAX_DELAY_SECS)
        log(f"retry-pending: still deferred ({pub.get('reason')}); "
            f"next attempt in ~{delay // 60}m")
        return 0
    # "failed": logic/validation/credential problem -- needs a human.
    return record_failure(pub.get("reason", "retry-failed"), 2, std,
                          {"attempts": attempts + 1,
                           "extra": pub.get("extra")})


def run_publish_phase(args, env, state, std, updated_ccns, unpublished,
                      index_ccns, pre_count, errors, health, wave_no):
    """Build, validate, publish, and live-verify one pricing update.

    VM-ONLY (2026-10-01): every R2 operation runs on the VM with adaptive
    retry; the Mac mini is in no R2 path. Returns a result dict:
      {"status": "published", "out": Path, "publish_ccns": [...]}
      {"status": "deferred", "reason":..., "detail":...}  -- R2 unreachable;
          CCNs are parked in state["unpublished"] + the pending manifest for
          the retry loop. NOT a wave failure (caller must not strike).
      {"status": "failed", "reason":..., "detail":...}    -- logic/validation/
          credential failure. Caller records a strike.
      {"status": "skipped"}  -- nothing to do (dry run or no CCNs).
    Safety invariants (unchanged): never publish a partial generation, never
    delete R2 objects, manifest uploaded last, byte-verify every object.
    """
    if not ((updated_ccns or unpublished) and not args.dry_run):
        return {"status": "skipped"}
    publish_ccns = list(dict.fromkeys(updated_ccns + unpublished))

    def _park(reason, detail, attempts):
        state["unpublished"] = list(dict.fromkeys(
            (state.get("unpublished") or []) + publish_ccns))
        save_state(state)
        park_for_retry(publish_ccns, wave_no, reason, detail, attempts)
        return {"status": "deferred", "reason": reason, "detail": detail,
                "publish_ccns": publish_ccns}

    def _fail(reason, detail, extra=None):
        rec = {"updated_ccns": publish_ccns, "detail": detail}
        if extra:
            rec.update(extra)
        return {"status": "failed", "reason": reason, "detail": detail,
                "publish_ccns": publish_ccns, "extra": rec}

    # 2026-09-27 fix: the local ~/hospital-ledger/ working copy is stale
    # pre-Tier-3 (26 hospitals). Sync the canonical Tier-3 index/summary
    # from R2 BEFORE build_site_data.py, or the wave would publish the
    # stale 26-hospital base and wipe 3,810 hospitals from the live site.
    # 2026-09-29 fix: this sync OVERWRITES public/data/prices/index.json,
    # so it MUST run before patch_index_from_prices.py. Patch publish_ccns
    # (not just updated_ccns): the sync also wipes merges carried over from
    # earlier no-publish waves, so the "already patched" skip is not valid.
    _renv = dict(env)
    _renv.update(load_r2_env())
    _renv["R2_BUCKET"] = "hl-mrf-parsed"
    # 2026-10-01: meta/summary.json dropped from this sync. build_site_data.py
    # REGENERATES public/data/summary.json from the index (never reads the
    # input), so syncing it was dead weight -- and its S3-only availability
    # (CDN 404s on meta/*) was single-handedly blocking every publish during
    # the egress outage. prices/index.json stays: the build reads it.
    for _key, _dest in [("prices/index.json", ROOT / "public/data/prices/index.json")]:
        _ok, _via = r2_get_file_fallback(_key, _dest, _renv, health)
        if not _ok:
            return _park("site-data-build-fail",
                         f"cannot sync {_key} from R2 (via={_via})",
                         (load_pending() or {}).get("attempts", 0))
        # 2026-10-13: a synced base is not trusted until it matches the
        # live manifest's artifact record for the key. The CDN fallback
        # can serve a stale site-bundled copy for this key; building on
        # it regresses the index. Mismatch -> park (transport class),
        # never build -- UNLESS verify_synced_base's 2026-10-23
        # manifest-stale discriminator proves the base is the canonical
        # R2 object (dual-leg byte-identical + sane) and the manifest is
        # the stale artifact, in which case the publish below heals R2
        # by regenerating the manifest last.
        _bok, _bdetail = verify_synced_base(_key, _dest, via=_via, renv=_renv)
        if not _bok:
            return _park("base-verify-fail", _bdetail,
                         (load_pending() or {}).get("attempts", 0))
        log(f"base verify: {_bdetail}")
        log(f"synced {_key} from R2 before build (via {_via})")
    if publish_ccns:
        env2 = dict(env)
        env2["CCNS"] = ",".join(publish_ccns)
        r1 = subprocess.run([VENV_PY, "scripts/patch_index_from_prices.py"],
                            cwd=str(ROOT), env=env2, capture_output=True, text=True, timeout=300)
        log(f"patch_index rc={r1.returncode}")
        if r1.returncode != 0:
            return _fail("site-data-build-fail", r1.stderr[-600:],
                         {"patch_rc": r1.returncode})
    r2 = subprocess.run([VENV_PY, "scripts/build_site_data.py"],
                        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    log(f"build_site_data rc={r2.returncode}")
    if r2.returncode != 0:
        return _fail("site-data-build-fail", r2.stderr[-600:],
                     {"build_rc": r2.returncode})

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = ROOT / "data" / f"wave-out-{ts}"
    (out / "meta").mkdir(parents=True)
    (out / "prices").mkdir(parents=True)
    for src, dst in [("public/data/summary.json", "meta/summary.json"),
                     ("public/data/hospitals.json", "meta/hospitals.json"),
                     ("public/data/prices/index.json", "prices/index.json")]:
        (out / dst).write_bytes((ROOT / src).read_bytes())
    for ccn in publish_ccns:
        (out / "prices" / f"{ccn}.json").write_bytes(
            (ROOT / "public/data/prices" / f"{ccn}.json").read_bytes())
    git_sha = mini_git_head()
    if git_sha:
        log(f"manifest git_commit={git_sha[:7]}")
    else:
        log("manifest git_commit unavailable (mini unreachable) -- continuing without")
    mk_args = [VENV_PY, "scripts/make_manifest.py", str(out),
               "--producer", PRODUCER, "--tier", "counts",
               "--note", f"wave {wave_no} pricing updated_ccns={','.join(publish_ccns)}"]
    if git_sha:
        mk_args += ["--git-commit", git_sha, "--git-repo", GIT_REPO_URL]
    subprocess.run(mk_args,
                   cwd=str(ROOT), check=True, capture_output=True, text=True, timeout=120)

    v = subprocess.run([VENV_PY, "scripts/validate_site_data.py", str(out),
                        "--tier", "counts", "--quiet"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    validation = "PASS" if v.returncode == 0 else "FAIL"
    log(f"counts-tier validation: {validation}")
    if v.returncode != 0:
        return _fail("counts-validation-fail", v.stderr[-500:])

    ok, msg = validate_pricing(out, publish_ccns, pre_count, index_ccns)
    pricing_validation = "PASS" if ok else "FAIL"
    log(f"pricing-tier validation: {pricing_validation} ({msg})")
    if not ok:
        return _fail("pricing-validation-fail", msg)

    if args.no_publish:
        log("--no-publish: assembled + validated, not uploaded")
        state["unpublished"] = list(dict.fromkeys(unpublished + updated_ccns))
        save_state(state)
        return {"status": "skipped", "publish_ccns": publish_ccns,
                "validation": validation,
                "pricing_validation": pricing_validation}

    penv = dict(env)
    penv.update(load_r2_env())
    penv["R2_BUCKET"] = "hl-mrf-parsed"
    # Pre-publish check: 403 = credential rejection (terminal, needs a human).
    # Any other check failure is treated as transport: the attempt loop below
    # (with backoff + health gate) makes the final call and defers on failure.
    chk = subprocess.run([VENV_PY, "scripts/r2_put.py", "--check"],
                         cwd=str(ROOT), env=penv,
                         capture_output=True, text=True, timeout=60)
    chk_out = (chk.stdout or "") + (chk.stderr or "")
    if chk.returncode != 0 and "403" in chk_out:
        return _fail("r2-credential-rejected", chk_out[-300:])
    if chk.returncode != 0:
        log("R2 pre-check failed (transport suspected); proceeding to publish attempts")
    ok, msg = _publish_with_retry(out, publish_ccns, penv, health)
    log(f"publish: {msg}")
    if not ok:
        # Preserve the work: the CCNs were reparsed and validated; a
        # transient publish failure must not lose them. Park for the retry
        # loop instead of failing the wave.
        return _park("publish-fail", msg,
                     (load_pending() or {}).get("attempts", 0))
    want = json.loads((out / "meta/summary.json").read_text())["generated_at"]
    live_ok = live_verify_manifest(want, publish_ccns, out)
    log(f"live verify: {'OK' if live_ok else 'FAILED'}")
    if not live_ok:
        # Bytes are on R2 (byte-verified); the site check failed. Park for
        # the retry loop, which re-verifies (re-publish is idempotent).
        return _park("live-verify-fail",
                     "published bytes verified in R2 but the live manifest check failed",
                     (load_pending() or {}).get("attempts", 0))
    prov_ok, prov_msg = git_provenance_commit(
        out, {"wave": wave_no, "generated_at": want,
              "published_ccns": publish_ccns})
    log(f"git provenance: {prov_msg}")
    # Publish fully verified: record per-CCN hashes so future drift between
    # local files and R2 is self-healing, then clear the backlog.
    baseline = load_published_hashes()
    for ccn in publish_ccns:
        try:
            baseline[ccn] = hashlib.sha256(
                (ROOT / "public/data/prices" / f"{ccn}.json").read_bytes()).hexdigest()
        except OSError:
            pass
    save_published_hashes(baseline)
    state["unpublished"] = []
    clear_pending()
    save_state(state)
    return {"status": "published", "out": out, "publish_ccns": publish_ccns,
            "validation": validation, "pricing_validation": pricing_validation}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave-size", type=int, default=50)
    ap.add_argument("--max-reparse", type=int, default=8)
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-ccn", default=None,
                    help="force one granted-host CCN through the full reparse chain")
    ap.add_argument("--retry-pending", action="store_true",
                    help="retry the pending publish manifest only (no wave); "
                         "exits 0 when nothing is due")
    args = ap.parse_args()

    strip_ipv6_no_proxy()
    std = load_std_loop()
    load_cors_proxy_key()  # proxy fallback armed iff HL_CORS_PROXY_KEY present
    if args.retry_pending:
        # The retry publisher is transport-only and drains an already-safe
        # backlog; it must keep running even when the research loop's
        # standard gate has stopped new waves.
        return retry_pending_publish(args, dict(os.environ), std)
    reason = check_std_gate(std)
    if reason:
        print(f"LOOP STOPPED by standard gate ({reason}). Manual review required.",
              file=sys.stderr)
        return 3

    edge = load_edge()
    if edge is not None and edge.enabled():
        try:
            log(edge.preflight())
        except Exception as e:
            return record_failure(f"edge-proxy-preflight: {e}", 2, std)
    granted = load_granted(edge)

    qdoc = json.loads(QUEUE_PATH.read_text()) if QUEUE_PATH.exists() else {}
    queue = qdoc.get("queue") or build_queue(granted)
    # Rebuild when the queue was built for the other mode (granted-only vs all hosts).
    want_mode = "all" if granted is None else "granted"
    if not queue or qdoc.get("mode", "granted") != want_mode or (granted is not None and not all(
            (urllib.parse.urlparse(r["url"]).hostname or "").lower() in granted
            for r in queue[:50])):
        queue = build_queue(granted)

    state = load_state()
    n = len(queue)
    pos = state["pos"] % n if n else 0
    # Auto-learned route health (2026-10-01): loaded once per wave, consulted
    # by every network leg (MRF fetch legs in detect_changes, R2 get/put in
    # the publish phase), saved after the publish phase.
    health = load_route_health()

    # Snapshot the index before any mutation (regression restore point).
    index_ccns, pre_count = load_index_ccns()
    # 2026-09-29: fail fast on unreadable/corrupt index. Treating it as empty
    # silently mis-baselines change detection (every URL looks new) and risks
    # publishing from a bad base. The Tier-3 index carries ~3,810 hospitals;
    # anything under 3,000 is corrupt or stale and must not proceed.
    if pre_count < 3000:
        return record_failure("index-unreadable", 2, std,
                              {"detail": f"local index has {pre_count} hospitals (<3000); refusing to wave on a corrupt base"})
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
    with httpx.Client(timeout=150, follow_redirects=True, headers=UA,
                      **(edge.httpx_kwargs() if edge else {})) as c:
        changed, errors, resolved, dead = detect_changes(c, wave_urls, state, index_ccns, health)
    log(f"checked={len(wave_urls)} resolved={resolved} changed={len(changed)} "
        f"errors={len(errors)} dead={len(dead)}")

    # Pervasive probe failure: do not advance the watermark; fail the wave.
    # Minimum sample 20 (2026-10-25): at tiny windows the 50% ratio is a
    # statistical artifact - a wave-size-1 force run whose single window row
    # returns an origin 403 trips the guard, burns a loop strike, and on
    # 2026-10-25 stopped the standing loop (wave 48, CCN 194112, 403 via
    # proxy) while the forced row itself never got probed. Below 20 rows a
    # failed probe is just a row error; the wave proceeds.
    if len(wave_urls) >= 20 and resolved / len(wave_urls) < 0.5:
        save_state(state)  # persist per-URL outcomes, but keep pos
        save_route_health(health)  # persist what the fetch legs just learned
        return record_failure("pervasive-probe-failure", 2, std,
                              {"checked": len(wave_urls), "resolved": resolved,
                               "errors": [e["detail"] for e in errors[:5]]})

    # Reparse: deferred first, then newly changed, then forced (forced always runs).
    to_parse = list(deferred) + changed
    if forced and forced["ccn"] not in {d.get("ccn") for d in to_parse}:
        to_parse.append(forced)
    updated_ccns, still_deferred = [], []
    if to_parse and not args.dry_run:
        updated_ccns, still_deferred = reparse_changed(to_parse, args.max_reparse, errors, state)
        ok, count = verify_index_complete(pre_count, index_ccns, updated_ccns)
        if not ok:
            try:
                shutil.copy2(snapshot, INDEX_PATH)
                log("index restored from pre-wave snapshot")
            except OSError as e:
                log(f"snapshot restore FAILED: {e}")
            save_route_health(health)
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
    # Self-healing drift net (2026-10-01): any local price file that differs
    # from its last-published hash rejoins the publish backlog, and any
    # baseline CCN whose file vanished is requeued for reparse (change
    # detection cannot re-discover it -- the bytes were already seen).
    _baseline = load_published_hashes()
    _drifted = [c for c in find_drifted_ccns(_baseline)
                if (ROOT / "public/data/prices" / f"{c}.json").exists()]
    if _drifted:
        log(f"drift net: {len(_drifted)} CCNs differ from last-published hash; re-queueing for publish")
    _missing = find_missing_ccns(_baseline)
    if _missing:
        _by_ccn = {r["ccn"]: r for r in queue}
        _requeue = [_by_ccn[c] for c in _missing if c in _by_ccn]
        _no_row = [c for c in _missing if c not in _by_ccn]
        if _requeue:
            state["deferred"] = _requeue + (state.get("deferred") or [])
            log(f"drift net: {len(_requeue)} CCNs with vanished price files requeued for reparse")
        if _no_row:
            log(f"WARNING: {len(_no_row)} vanished-file CCNs have no queue row and cannot be requeued: {_no_row}")
    unpublished = list(dict.fromkeys(unpublished + _drifted))
    # Persist the pruned carry list even when this wave publishes nothing:
    # a CCN whose price file vanished must not linger in state forever.
    if unpublished != (state.get("unpublished") or []):
        state["unpublished"] = unpublished
        save_state(state)
    publish_ccns = []
    # Adopt any pending publish manifest (2026-10-01): CCNs parked by an
    # earlier wave whose publish could not reach R2 ride along in this wave.
    # The manifest is cleared only inside run_publish_phase on verified
    # success; on deferral it is re-parked with attempts preserved.
    _pending = load_pending()
    if _pending:
        _add = [c for c in (_pending.get("publish_ccns") or [])
                if (ROOT / "public/data/prices" / f"{c}.json").exists()]
        if _add:
            log(f"adopting {len(_add)} CCNs from pending publish manifest")
            unpublished = list(dict.fromkeys(unpublished + _add))
    env = dict(os.environ)
    pub = run_publish_phase(args, env, state, std, updated_ccns, unpublished,
                            index_ccns, pre_count, errors, health,
                            state["waves_done"] + 1)
    save_route_health(health)
    status = pub.get("status")
    if status == "failed":
        return record_failure(pub["reason"], 2, std, pub.get("extra"))
    # A deferred publish is transport-owned (the retry loop recovers it):
    # the wave still counts as run, but no_progress_streak is left untouched
    # -- neither a research success nor a research failure.
    deferred_wave = (status == "deferred")
    if deferred_wave:
        record_deferred(pub["reason"], std, pub.get("detail", ""))
        publish_ccns = pub.get("publish_ccns", [])
    else:
        publish_ccns = pub.get("publish_ccns", [])
        published = (status == "published")
        validation = pub.get("validation", validation)
        pricing_validation = pub.get("pricing_validation", pricing_validation)
        out = pub.get("out")

    # Watermark advance (only reached on non-terminal waves).
    state["pos"] = (pos + len(wave_urls)) % n
    state["waves_done"] += 1
    state["deferred"] = still_deferred
    save_state(state)
    if deferred_wave:
        std["iter"] = int(std.get("iter", 0) or 0) + 1
        save_std_loop(std)
    else:
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
