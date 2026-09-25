#!/usr/bin/env python3
"""vm_wave.py — one rebuild wave, fully VM-local (gen-20260923-vm).

Usage:  ~/hospital-ledger/.venv/bin/python3 vm_wave.py <W> [--workers N]

Pipeline per wave W (1..11):
  0. Pre-flight: df >= 25 GB free; every R2 key asserted under the staging prefix.
  1. Stream: batch-download parsed/{ccn}.json for the wave's CCNs via the
     persistent-connection client (Content-Length + SHA-256 verified).
  2. Missing-CCN probe: for any CCN missing/failed in R2, look up its MRF URL
     (mrf_probe table), probe it with hl_wave semantics (httpx, 750 MB
     mid-stream cap), and re-parse via mrf_parse.py (venv python, own caps).
  3. Slim: full-mode slim_parsed.py (copied into the isolated wave repo so its
     ROOT is the wave dir; NO CCNS filter -> emits prices, index, cpt-index,
     _cpt_detail_raw.jsonl, payer + compliance sidecars).
  4. Gates (§2c): parse health (>20% MRF failures -> wave fails), standardize
     health (0-row CCNs quarantined; >5% -> wave fails), CPT universe
     (unknown-code spike -> wave fails).
  5. Stage: upload prices/{ccn}.json + wave fragments to
     gen/gen-20260923-vm/... with byte-verified PUTs; waves/W/manifest.json LAST.
  6. Cleanup: rm -rf the wave dir only after the manifest is staged.

Writes: /home/hatch/hl-vm-waves/results/w<W>.json, progress-log lines,
        and updates wave-orchestrator-status.json itself (single writer).
Exit 0 = wave staged+verified; non-zero = wave failed (status halted).
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import datetime

# ---------------------------------------------------------------- constants
W = int(sys.argv[1])
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--workers" else 8

GEN_ID = "gen-20260923-vm"
PREFIX = f"gen/{GEN_ID}/"
HF = os.path.expanduser("~/workspace/goals/hospital-ledger-weekly-refresh-counts-tier/hidden_files")
WAVES_DIR = "/home/hatch/hl-vm-waves"
WDIR = os.path.join(WAVES_DIR, f"w{W}")
RESULTS_DIR = os.path.join(WAVES_DIR, "results")
SCRIPTS = os.path.expanduser("~/hospital-ledger/scripts")
VENV_PY = os.path.expanduser("~/hospital-ledger/.venv/bin/python3")
DB = os.path.expanduser("~/hospital-ledger/db/hospital_ledger.db")
STATUS = os.path.join(HF, "wave-orchestrator-status.json")
PROGRESS = os.path.join(HF, "vm-rebuild-progress.log")
UNIVERSE = json.load(open(os.path.join(HF, "cpt-universe.json")))
MIN_FREE = 25 * 1024 ** 3
PROBE_CAP = 750 * 1024 * 1024  # hl_wave.py MAX_BYTES semantics

sys.path.insert(0, HF)
from vm_r2client import R2Client  # noqa: E402

t0 = time.time()


def log(msg):
    line = f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}] [wave{W}] {msg}"
    print(line, flush=True)
    with open(PROGRESS, "a") as fh:
        fh.write(line + "\n")


def df_free(path=WAVES_DIR):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def guard_key(key):
    if not key.startswith(PREFIX):
        raise RuntimeError(f"REFUSED: key outside staging prefix: {key}")


def update_status(**kw):
    s = json.load(open(STATUS))
    s.update(kw)
    s["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    json.dump(s, open(STATUS, "w"), indent=1)


def fail(reason):
    # Records the failure and exits non-zero. Does NOT set halted: the
    # coordinator owns the retry budget (stage-resume -> full re-run, halt
    # only after 5 consecutive failures) per Barklee's 2026-09-24 auto-fix
    # directive. The setup watchdog pages only on a real halt.
    log(f"WAVE FAILED: {reason}")
    update_status(in_flight=[],
                  failed=sorted(set(json.load(open(STATUS)).get("failed", [])) | {W}))
    res = {"wave": W, "verdict": "fail", "reason": reason,
           "elapsed_s": round(time.time() - t0, 1)}
    json.dump(res, open(os.path.join(RESULTS_DIR, f"w{W}.json"), "w"), indent=1)
    sys.exit(1)


# ---------------------------------------------------------------- helpers shared by full + resume-stage paths
def _local_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_uploads(uploads, verify_first=False):
    """Byte-verified staging of (local, key) pairs.

    verify_first=True: length-checked GET first; PUT only when the remote
    object is missing or differs (idempotent resume -- skips the ~24 min
    re-PUT of objects a failed attempt already staged).
    Returns (staged, stage_fail).
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor
    staged, stage_fail = [], []
    _tls = threading.local()

    def _uput():
        c = getattr(_tls, "client", None)
        if c is None:
            c = _tls.client = R2Client(workers=1)
        return c

    def _one(pair):
        local, key = pair
        try:
            if verify_first:
                lh = _local_sha(local)
                vok, vinfo = _uput().get_sha256(
                    key, expect_len=os.path.getsize(local))
                if vok and vinfo == lh:
                    return True, lh, local, key
            ok, info = _uput().put_verified(local, key)
            return ok, info, local, key
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:200], local, key

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for ok, info, local, key in ex.map(_one, uploads):
            if ok:
                staged.append({"key": key, "bytes": os.path.getsize(local),
                               "sha256": info})
            else:
                stage_fail.append({"key": key, "err": info})
                log(f"  UPLOAD FAIL {key}: {info}")
    log(f"stage: {len(staged)}/{len(uploads)} ok, {len(stage_fail)} failed")
    return staged, stage_fail


def finalize_wave(ctx, staged):
    """Stage the wave manifest LAST, retain build cache, clean up, mark done."""
    manifest_doc = {
        "gen_id": GEN_ID,
        "wave": W,
        "ccns": sorted(ctx["have_inputs"]),
        "n_ccns": len(ctx["have_inputs"]),
        "downloaded": sorted(ctx["dl_ok"]),
        "probed": sorted(ctx["probed_ok"]),
        "still_missing": ctx["still_missing"],
        "quarantined": ctx["quarantined"],
        "known_quarantined": ctx.get("known_quarantined", []),
        "standardized_rows": ctx["std_rows"],
        "download_bytes": ctx["download_bytes"],
        "cpt_format": {"distinct_codes": ctx["cpt"]["distinct"],
                       "malformed_codes": ctx["cpt"]["malformed_codes"],
                       "malformed_rows": ctx["cpt"]["malformed_rows"],
                       "outside_live_top5k": ctx["cpt"]["outside_live_top5k"],
                       "verdict": "pass"},
        "artifacts": staged,
        "verdict": "pass",
        "mode": ctx.get("mode", "full"),
        "started_at": ctx["t0_iso"],
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    mbody = json.dumps(manifest_doc, indent=1).encode()
    mtmp = os.path.join(WDIR, "manifest.json")
    open(mtmp, "wb").write(mbody)
    mkey = PREFIX + f"waves/{W}/manifest.json"
    guard_key(mkey)
    ok, minfo = R2Client(workers=1).put_verified(mtmp, mkey)
    if not ok:
        fail(f"staging: wave manifest upload failed: {minfo}")
    staged.append({"key": mkey, "bytes": len(mbody), "sha256": minfo})
    log(f"stage: wave manifest staged ({mkey})")

    # Retain merge inputs locally (build cache): prices/ + fragments. The
    # bulky parsed/ downloads and tmp/ spill are deleted to bound disk.
    KEEP = os.path.join(WAVES_DIR, "keep", f"w{W}")
    os.makedirs(KEEP, exist_ok=True)
    for sub in ("public", "data"):
        src = os.path.join(WDIR, sub)
        if os.path.exists(src):
            shutil.move(src, os.path.join(KEEP, sub))
    shutil.rmtree(WDIR, ignore_errors=True)
    res = {"wave": W, "verdict": "pass",
           "n_ccns": len(ctx["have_inputs"]),
           "downloaded": len(ctx["dl_ok"]), "probed": len(ctx["probed_ok"]),
           "still_missing": ctx["still_missing"],
           "quarantined": ctx["quarantined"],
           "known_quarantined": ctx.get("known_quarantined", []),
           "standardized_rows": ctx["std_rows"],
           "download_bytes": ctx["download_bytes"],
           "staged": len(staged), "kept": KEEP,
           "mode": ctx.get("mode", "full"),
           "elapsed_s": round(time.time() - t0, 1),
           "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    json.dump(res, open(os.path.join(RESULTS_DIR, f"w{W}.json"), "w"), indent=1)
    s = json.load(open(STATUS))
    s["done"] = sorted(set(s.get("done", [])) | {W})
    s["failed"] = sorted(set(s.get("failed", [])) - {W})
    s["in_flight"] = []
    s["cursor"] = W + 1
    s["consecutive_failures"] = 0
    s["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    json.dump(s, open(STATUS, "w"), indent=1)
    free_after = df_free()
    log(f"WAVE {W} PASS ({ctx.get('mode', 'full')}): {len(ctx['have_inputs'])} CCNs, "
        f"{ctx['std_rows']} rows, {len(staged)} staged, "
        f"{res['elapsed_s'] / 3600:.2f}h elapsed, df free={free_after / 1e9:.1f} GB")


def build_uploads():
    """(local, key) staging list from the wave workdir. Shared by both paths."""
    prices_dir = os.path.join(WDIR, "public", "data", "prices")
    index_path = os.path.join(prices_dir, "index.json")
    payer_raw = os.path.join(WDIR, "data", "_payer_raw.jsonl")
    compl_raw = os.path.join(WDIR, "data", "_compliance_per_hospital.jsonl")
    detail_raw = os.path.join(WDIR, "data", "_cpt_detail_raw.jsonl")
    wave_cpt_index = os.path.join(WDIR, "public", "data", "cpt-index.json")
    uploads = []
    for f in sorted(os.listdir(prices_dir)):
        if f.endswith(".json") and f != "index.json":
            uploads.append((os.path.join(prices_dir, f), PREFIX + f"prices/{f}"))
    for local, key in ((detail_raw, PREFIX + f"waves/{W}/_cpt_detail_raw.jsonl"),
                       (payer_raw, PREFIX + f"waves/{W}/_payer_raw.jsonl"),
                       (compl_raw, PREFIX + f"waves/{W}/_compliance_per_hospital.jsonl"),
                       (index_path, PREFIX + f"waves/{W}/prices-index.json"),
                       (wave_cpt_index, PREFIX + f"waves/{W}/cpt-index.json")):
        uploads.append((local, key))
    for _, key in uploads:
        guard_key(key)
    return uploads


def resume_stage():
    """--resume-stage: re-run ONLY staging from a failed wave's workdir.

    Used by the coordinator's auto-fix ladder for staging-only failures:
    verify-first re-stage (skips objects the failed attempt already got
    right), stage the manifest LAST, retain, mark done. Exits 0 on pass,
    1 via fail() otherwise.
    """
    os.makedirs(WDIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    free = df_free()
    if free < MIN_FREE:
        fail(f"insufficient disk: {free / 1e9:.1f} GB < 25 GB")
    # Prerequisite check BEFORE any status write: a failed probe must never
    # clear a legitimate halt (2026-09-24: a probe cleared halted and systemd
    # relaunched the coordinator into a full re-run).
    state_path = os.path.join(WDIR, "stage-state.json")
    if not os.path.exists(state_path):
        fail(f"resume-stage: no stage-state.json in {WDIR} (workdir incomplete?)")
    update_status(in_flight=[W], cursor=W, halted=False)
    ctx = json.load(open(state_path))
    ctx["mode"] = "resume-stage"
    uploads = build_uploads()
    missing = [local for local, _ in uploads if not os.path.exists(local)]
    if missing:
        fail(f"resume-stage: {len(missing)} upload sources missing, e.g. {missing[0]}")
    log(f"resume-stage: {len(uploads)} objects (verify-first, idempotent)")
    staged, stage_fail = stage_uploads(uploads, verify_first=True)
    if stage_fail:
        fail(f"staging: {len(stage_fail)} uploads failed, e.g. {stage_fail[0]}")
    finalize_wave(ctx, staged)


# ---------------------------------------------------------------- 0. pre-flight
# --resume-stage: skip the pipeline, re-run only staging from a failed
# attempt's workdir (coordinator auto-fix ladder).
if len(sys.argv) > 2 and sys.argv[2] == "--resume-stage":
    if len(sys.argv) > 4 and sys.argv[3] == "--workers":
        WORKERS = int(sys.argv[4])
    log(f"resume-stage requested for wave {W}")
    resume_stage()
    sys.exit(0)
os.makedirs(WDIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)
free = df_free()
log(f"preflight: df free={free / 1e9:.1f} GB (floor 25 GB)")
if free < MIN_FREE:
    fail(f"insufficient disk: {free / 1e9:.1f} GB < 25 GB")
plan = json.load(open(os.path.join(HF, "wave-plan-11.json")))
wave = next(w for w in plan["waves"] if w["wave"] == W)
ccns = wave["ccns"]
log(f"preflight: wave {W}: {len(ccns)} CCNs, plan bytes={wave['bytes_raw'] / 1e9:.2f} GB")
update_status(in_flight=[W], cursor=W, halted=False)

# ---------------------------------------------------------------- 1. stream
# NOTE: downloads go DIRECTLY to data/parsed/ — that is where slim_parsed.py
# reads its inputs. (An earlier draft used WDIR/parsed/ and slim saw nothing.)
pdir = os.path.join(WDIR, "data", "parsed")
os.makedirs(pdir, exist_ok=True)
manifest = [{"key": f"parsed/{c}.json", "dest": os.path.join(pdir, f"{c}.json")}
            for c in ccns]
for m in manifest:
    if not m["key"].startswith("parsed/"):
        raise RuntimeError(f"REFUSED: unexpected source key {m['key']}")
client = R2Client(workers=WORKERS)
dl_ok, dl_fail, dl_bytes = {}, [], 0
from concurrent.futures import ThreadPoolExecutor


def _pipe_ok():
    """Single small GET with a 60s budget; True iff the R2 pipe is alive."""
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(HF, "vm_r2client.py"),
             "get", "meta/manifest.json", "/tmp/vm-wave-pipe-probe.json"],
            capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _wait_for_pipe():
    while not _pipe_ok():
        log("pipe down; waiting 5 min before re-probe")
        time.sleep(300)
    log("pipe recovered")


def _dl_one(m):
    ccn = os.path.basename(m["dest"])[:-5]
    try:
        ok, info, n = client.get_file(m["key"], m["dest"])
    except Exception as e:
        ok, info, n = False, f"{type(e).__name__}: {e}"[:200], 0
    if not ok:
        # one immediate retry on a fresh connection before giving up this round
        try:
            ok, info, n = R2Client(workers=1).get_file(m["key"], m["dest"])
        except Exception as e:
            ok, info, n = False, f"{type(e).__name__}: {e}"[:200], 0
    return m["key"], ccn, ok, info, n


# Round-based download: each round attempts all remaining files. A round
# with zero progress means the pipe is down -> wait for recovery before
# the next round. Survives flapping without failing the wave.
remaining = {m["key"]: m for m in manifest}
round_n = 0
while remaining and round_n < 6:
    round_n += 1
    if not _pipe_ok():
        log(f"download round {round_n}: pipe down, waiting for recovery")
        _wait_for_pipe()
    log(f"download round {round_n}: {len(remaining)} files remaining")
    made_progress = False
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_dl_one, m): m for m in remaining.values()}
        for f in futs:
            key, ccn, ok, info, n = f.result()
            if ok:
                dl_ok[ccn] = {"sha256": info, "bytes": n}
                dl_bytes += n
                del remaining[key]
                made_progress = True
    log(f"download round {round_n}: {len(dl_ok)}/{len(ccns)} ok total, "
        f"{len(remaining)} remaining")
    if remaining and not made_progress:
        log(f"download round {round_n}: no progress; waiting for pipe recovery")
        _wait_for_pipe()
for key, m in remaining.items():
    ccn = os.path.basename(m["dest"])[:-5]
    dl_fail.append({"ccn": ccn, "err": "failed after 6 download rounds"})
log(f"download: {len(dl_ok)}/{len(ccns)} ok, {dl_bytes / 1e9:.2f} GB, "
    f"{len(dl_fail)} failed, {time.time() - t0:.0f}s elapsed")

# ---------------------------------------------------------------- 2. missing-CCN probe
probed_ok, still_missing = [], []


def probe_url(url):
    """hl_wave.detect_changes semantics: stream with 750 MB mid-stream cap."""
    import httpx
    try:
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (compatible; HospitalLedger/1.0)"},
                          follow_redirects=True, timeout=60.0, verify=False) as c:
            with c.stream("GET", url) as r:
                sc = r.status_code
                if sc in (404, 410):
                    return ("dead", f"http {sc}")
                if sc != 200:
                    return ("error", f"http {sc}")
                n = 0
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    n += len(chunk)
                    if n > PROBE_CAP:
                        return ("too_large", f">{PROBE_CAP} bytes")
                return ("alive", f"{n} bytes")
    except Exception as e:
        return ("error", f"{type(e).__name__}: {str(e)[:100]}")


if dl_fail:
    db = sqlite3.connect(DB)
    # isolated wave repo for mrf_parse (its ROOT derives from script location)
    wscripts = os.path.join(WDIR, "scripts")
    os.makedirs(wscripts, exist_ok=True)
    shutil.copy(os.path.join(SCRIPTS, "mrf_parse.py"), wscripts)
    os.makedirs(os.path.join(WDIR, "data", "parsed"), exist_ok=True)
    for fm in dl_fail:
        ccn = fm["ccn"]
        row = db.execute(
            "select mrf_url from mrf_probe where ccn=? order by alive desc, probed_at desc limit 1",
            (ccn,)).fetchone()
        name = db.execute("select name from hospitals where ccn=?", (ccn,)).fetchone()
        name = name[0] if name else ccn
        if not row or not row[0]:
            still_missing.append({"ccn": ccn, "reason": "no mrf_url in mrf_probe"})
            continue
        url = row[0]
        verdict, detail = probe_url(url)
        log(f"probe {ccn}: {verdict} ({detail}) {url[:80]}")
        if verdict != "alive":
            still_missing.append({"ccn": ccn, "reason": f"probe {verdict}: {detail}"})
            continue
        p = subprocess.run([VENV_PY, "scripts/mrf_parse.py", "--ccn", ccn,
                            "--name", name, "--url", url],
                           cwd=WDIR, capture_output=True, text=True, timeout=3600)
        out = (p.stdout or "") + (p.stderr or "")
        log(f"mrf_parse {ccn}: rc={p.returncode} {out.strip().splitlines()[-1][:120] if out.strip() else ''}")
        ppath = os.path.join(WDIR, "data", "parsed", f"{ccn}.json")
        if p.returncode == 0 and os.path.exists(ppath) and os.path.getsize(ppath) > 0:
            # ppath IS data/parsed/{ccn}.json == pdir; already in place.
            probed_ok.append(ccn)
        else:
            still_missing.append({"ccn": ccn, "reason": f"mrf_parse rc={p.returncode}"})
    db.close()

n_parsed_inputs = len(dl_ok) + len(probed_ok)
parse_fail_rate = len(still_missing) / len(ccns)
log(f"parse health: inputs={n_parsed_inputs}/{len(ccns)}, still-missing={len(still_missing)} "
    f"({parse_fail_rate * 100:.1f}%, gate: >20% fails the wave)")
if parse_fail_rate > 0.20:
    fail(f"parse health gate: {len(still_missing)}/{len(ccns)} MRFs unparseable (>20%)")

# ---------------------------------------------------------------- 3. slim (full mode)
wscripts = os.path.join(WDIR, "scripts")
os.makedirs(wscripts, exist_ok=True)
shutil.copy(os.path.join(SCRIPTS, "slim_parsed.py"), wscripts)
os.makedirs(os.path.join(WDIR, "tmp"), exist_ok=True)
env = dict(os.environ, TMPDIR=os.path.join(WDIR, "tmp"))
env.pop("CCNS", None)
env.pop("CCNS_FILE", None)
log(f"slim: full-mode run over {n_parsed_inputs} parsed files (TMPDIR=wave tmp)")
p = subprocess.run([sys.executable, "scripts/slim_parsed.py"], cwd=WDIR,
                   env=env, capture_output=True, text=True, timeout=14400)
tail = (p.stdout or "").strip().splitlines()[-8:]
log(f"slim: rc={p.returncode}")
for t in tail:
    log(f"  slim | {t[:160]}")
if p.returncode != 0:
    log(f"  slim stderr: {(p.stderr or '')[-500:]}")
    fail(f"slim_parsed.py rc={p.returncode}")

prices_dir = os.path.join(WDIR, "public", "data", "prices")
index_path = os.path.join(prices_dir, "index.json")
payer_raw = os.path.join(WDIR, "data", "_payer_raw.jsonl")
compl_raw = os.path.join(WDIR, "data", "_compliance_per_hospital.jsonl")
detail_raw = os.path.join(WDIR, "data", "_cpt_detail_raw.jsonl")
wave_cpt_index = os.path.join(WDIR, "public", "data", "cpt-index.json")
for req in (index_path, payer_raw, compl_raw, detail_raw, wave_cpt_index):
    if not os.path.exists(req) or os.path.getsize(req) == 0:
        fail(f"slim output missing/empty: {req}")

# ---------------------------------------------------------------- 4. gates
# slim writes {"hospitals": [...]} with per-CCN n (not n_items); index by CCN.
index = json.load(open(index_path))
index_by_ccn = {h.get("ccn"): h for h in index.get("hospitals", [])}
price_files = {f[:-5] for f in os.listdir(prices_dir) if f.endswith(".json") and f != "index.json"}
have_inputs = set(dl_ok) | set(probed_ok)
quarantined = sorted(c for c in have_inputs
                     if c not in price_files or index_by_ccn.get(c, {}).get("n", 0) == 0)
# Known-bad sources (operator-documented in known-quarantined.json, keyed by
# wave): hospitals whose published files contain zero usable price rows
# (placeholders, wrong formats, price-less CDM dumps). They are excluded
# from the gate's failure rate but stay recorded in stage-state.json.
# Reversible: delete a manifest entry and the gate trips on it again.
known_q = set()
try:
    _manifest = json.load(open(os.path.join(HF, "known-quarantined.json")))
    known_q = {e["ccn"] for e in _manifest.get(str(W), [])}
except FileNotFoundError:
    pass
known_hit = sorted(c for c in quarantined if c in known_q)
unexpected_q = [c for c in quarantined if c not in known_q]
# schema spot-check: every price file must be a JSON dict with an items list
bad_schema = []
for c in sorted(price_files):
    fp = os.path.join(prices_dir, f"{c}.json")
    try:
        d = json.load(open(fp))
        items = d.get("items")
        assert isinstance(d, dict) and isinstance(items, list)
        for it in items[:50]:
            assert isinstance(it, dict) and it.get("code")
    except Exception as e:
        bad_schema.append({"ccn": c, "err": f"{type(e).__name__}: {str(e)[:80]}"})
std_rows = sum(index_by_ccn.get(c, {}).get("n", 0) for c in price_files)
q_rate = len(unexpected_q) / max(1, n_parsed_inputs)
log(f"standardize: {len(price_files)} price files, {std_rows} rows, "
    f"quarantined={len(quarantined)} ({len(known_hit)} known-bad documented, "
    f"{len(unexpected_q)} unexpected, {q_rate * 100:.2f}%), bad_schema={len(bad_schema)}")
if bad_schema:
    fail(f"schema validation: {len(bad_schema)} price files invalid, e.g. {bad_schema[0]}")
if q_rate > 0.05:
    fail(f"standardize health gate: {len(unexpected_q)}/{n_parsed_inputs} unexpected quarantined (>5%)")

# CPT format gate: codes must be 5-char alphanumeric (CPT/HCPCS shape).
# NOTE: the earlier "universe" gate compared wave codes against the LIVE
# top-5,000 snapshot (cpt-universe.json) and would have falsely failed any
# wave containing legitimate codes outside that top-5k. There is no
# authoritative CPT/HCPCS universe file in the repo, so the gate checks
# FORMAT (catches garbage like "N/A" or description leaks) and reports
# live-top-5000 overlap as informational only. Valid codes are never
# discarded by this gate.
code_re = re.compile(r"^[A-Z0-9]{5}$")
wave_codes, badfmt_codes, badfmt_rows, total_rows = set(), set(), 0, 0
outside_live_top5k = 0
with open(detail_raw) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        code = rec.get("code", "")
        n_entries = len(rec.get("entries", []))
        total_rows += n_entries
        wave_codes.add(code)
        if not code_re.match(code):
            badfmt_codes.add(code)
            badfmt_rows += n_entries
        elif code not in UNIVERSE:
            outside_live_top5k += 1
u_rate = badfmt_rows / max(1, total_rows)
log(f"cpt format: {len(wave_codes)} distinct codes, malformed={len(badfmt_codes)} "
    f"codes / {badfmt_rows} rows ({u_rate * 100:.3f}%), "
    f"outside live-top-5000 (informational)={outside_live_top5k}")
if len(badfmt_codes) > 25 or u_rate > 0.02:
    fail(f"cpt format gate: {len(badfmt_codes)} malformed codes, "
         f"{u_rate * 100:.2f}% malformed rows (spike)")

# ---------------------------------------------------------------- 5. stage
# Persist everything resume-stage needs BEFORE any upload, so a staging-only
# failure can be healed without re-running download/parse/slim.
stage_ctx = {
    "have_inputs": sorted(have_inputs),
    "dl_ok": sorted(dl_ok),
    "probed_ok": sorted(probed_ok),
    "still_missing": still_missing,
    "quarantined": quarantined,
    "known_quarantined": known_hit,
    "std_rows": std_rows,
    "download_bytes": dl_bytes,
    "cpt": {"distinct": len(wave_codes),
            "malformed_codes": len(badfmt_codes),
            "malformed_rows": badfmt_rows,
            "total_rows": total_rows,
            "outside_live_top5k": outside_live_top5k},
    "mode": "full",
    "t0_iso": datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).isoformat(),
}
json.dump(stage_ctx, open(os.path.join(WDIR, "stage-state.json"), "w"), indent=1)
uploads = build_uploads()
log(f"stage: {len(uploads)} objects to {PREFIX} (byte-verified PUTs, {WORKERS} workers)")
staged, stage_fail = stage_uploads(uploads)
if stage_fail:
    fail(f"staging: {len(stage_fail)} uploads failed, e.g. {stage_fail[0]}")

finalize_wave(stage_ctx, staged)
