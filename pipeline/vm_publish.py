#!/usr/bin/env python3
"""Publish the merged Tier-3 generation to the live R2 root (bucket root keys).

Merged set = Tier-3 gen-20260923-vm outputs (3,699 hospitals, validated
--tier full) + the 111 live-carried price files that daily pricing waves added
after the Tier-3 snapshot (present in the live prices/index.json, absent from
the Tier-3 build). Result: 3,810 hospitals in prices/ + prices/index.json +
meta/. Aggregates/ and indexes/cpt-index.json are the validated Tier-3 build
outputs (built from the 3,699-hospital corpus); the 111 carried-forward
hospitals are present in prices/, the index, and meta/ -- documented here and
in the publish record, closable by the next full rebuild whose waves ingest
the live MRF list.

Publish order: every artifact verify-first to its root key, meta/manifest.json
LAST. The live API serves root keys directly (verified: /api/manifest ==
root meta/manifest.json, /api/cpt-index sha256 == root indexes/cpt-index.json).

Usage: HL_REPO=~/hospital-ledger python3 vm_publish.py
Requires R2 env (~/hospital-ledger.env sourced).
"""
import concurrent.futures
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.environ.get("HL_REPO", os.path.expanduser("~/hospital-ledger")), "scripts"))

REPO = os.environ.get("HL_REPO", os.path.expanduser("~/hospital-ledger"))
WAVES_DIR = os.path.expanduser("~/hl-vm-waves")
MERGE_DIR = os.path.join(WAVES_DIR, "merge")
PUB = os.path.join(WAVES_DIR, "publish")
PROGRESS = os.path.join(PUB, ".publish-progress.json")
ROLLBACK_DIR = os.path.join(PUB, "rollback-prev-live")

GEN_ID = "gen-20260926-vm-publish"
PRODUCER = "hl-vm-publish"
CONTRACT_VERSION = "1"
PUBLISH_PREFIXES = ("meta/", "prices/", "indexes/", "aggregates/")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[publish {utcnow()}] {msg}", flush=True)


def fail(reason):
    print(f"[publish {utcnow()}] PUBLISH FAIL: {reason}", flush=True)
    sys.exit(1)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    t0 = time.time()
    from vm_r2client import R2Client
    from patch_index_from_prices import summary_from_prices_file

    client = R2Client(workers=8)

    # ---- reboot checkpoint: if a previous run validated this exact layout,
    # skip straight to the resumable upload (steps 1-7 are deterministic).
    CHECKPOINT = os.path.join(PUB, ".publish-validated.json")
    manifest_path = os.path.join(PUB, "meta", "manifest.json")
    skip_to_upload = False
    artifacts = None
    if os.path.isfile(CHECKPOINT) and os.path.isfile(manifest_path):
        try:
            cp = json.load(open(CHECKPOINT))
            if sha256_file(manifest_path) == cp.get("manifest_sha"):
                artifacts = json.load(open(manifest_path))["artifacts"]
                skip_to_upload = True
                log(f"checkpoint hit: validated layout intact "
                    f"({len(artifacts)} artifacts), skipping to upload")
        except Exception:  # noqa: BLE001
            pass

    if not skip_to_upload:
        _build_layout(client, summary_from_prices_file)
        artifacts = json.load(open(manifest_path))["artifacts"]
        # record the validated checkpoint for reboot resume
        json.dump({"manifest_sha": sha256_file(manifest_path),
                   "validated_at": utcnow(), "gen_id": GEN_ID},
                  open(CHECKPOINT, "w"))
        log("validation checkpoint written")

    _publish_all(client, artifacts, manifest_path)
    elapsed = time.time() - t0
    # (publish record written inside _publish_all)
    log(f"publish driver done in {elapsed:.0f}s")


def _build_layout(client, summary_from_prices_file):

    # ------------------------------------------------ 1. derive the 111 live-only CCNs
    log("deriving live-only CCN set")
    ok, _, _ = client.get_file("prices/index.json", "/tmp/pub-live-index.json")
    if not ok:
        fail("could not fetch live prices/index.json")
    ok, _, _ = client.get_file("gen/gen-20260923-vm/prices/index.json", "/tmp/pub-newgen-index.json")
    if not ok:
        fail("could not fetch Tier-3 prices/index.json")
    live_ccns = {str(x["ccn"]) for x in json.load(open("/tmp/pub-live-index.json"))["hospitals"]}
    new_ccns = {str(x["ccn"]) for x in json.load(open("/tmp/pub-newgen-index.json"))["hospitals"]}
    live_only = sorted(live_ccns - new_ccns)
    log(f"live={len(live_ccns)} tier3={len(new_ccns)} live-only={len(live_only)}")
    if not live_only:
        fail("expected 111 live-only CCNs, found none -- live index changed? refusing to guess")

    # ------------------------------------------------ 2. assemble publish tree
    prices_dir = os.path.join(PUB, "prices")
    os.makedirs(prices_dir, exist_ok=True)
    # 2a. hardlink the 3,699 Tier-3 price files (read-only use; instant, no extra disk)
    log("linking Tier-3 price files")
    n_linked = 0
    for fn in os.listdir(os.path.join(MERGE_DIR, "prices")):
        if fn == "index.json" or not fn.endswith(".json"):
            continue
        dst = os.path.join(prices_dir, fn)
        if not os.path.exists(dst):
            os.link(os.path.join(MERGE_DIR, "prices", fn), dst)
            n_linked += 1
    log(f"linked {n_linked} Tier-3 price files")

    # 2b. download the live-only price files (parallel; existence+validity check)
    log(f"downloading {len(live_only)} live-only price files")
    def _dl(ccn):
        key, dst = f"prices/{ccn}.json", os.path.join(prices_dir, f"{ccn}.json")
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return (ccn, "cached", None)
        err = None
        for attempt in range(3):
            try:
                ok, sha, nbytes = client.get_file(key, dst)
            except Exception as e:  # noqa: BLE001
                ok, sha = False, f"get-raised: {e}"
            if ok and os.path.getsize(dst) > 0:
                try:
                    d = json.load(open(dst))
                    if d.get("ccn") == ccn and isinstance(d.get("items"), list):
                        return (ccn, "ok", sha)
                    err = "shape/ccn mismatch"
                except Exception as e:  # noqa: BLE001
                    err = f"json invalid: {e}"
            else:
                err = sha
            time.sleep(3)
        return (ccn, "FAILED", err)

    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for ccn, status, info in ex.map(_dl, live_only):
            if status == "FAILED":
                failed.append((ccn, info))
    if failed:
        fail(f"{len(failed)} live-only price files failed to download/validate: {failed[:5]}")
    log("all live-only price files present and valid")

    # ------------------------------------------------ 3. regenerate prices/index.json (3,810)
    log("regenerating prices/index.json from all price files")
    hospitals = []
    for fn in sorted(os.listdir(prices_dir)):
        if fn == "index.json" or not fn.endswith(".json"):
            continue
        s = summary_from_prices_file(__import__("pathlib").Path(prices_dir) / fn)
        if not s:
            fail(f"summary_from_prices_file failed for {fn}")
        hospitals.append(s)
    n_ccns = len(hospitals)
    log(f"built {n_ccns} hospital summaries")
    if n_ccns != len(new_ccns) + len(live_only):
        fail(f"expected {len(new_ccns) + len(live_only)} summaries, got {n_ccns}")
    gindex = {"hospitals": sorted(hospitals, key=lambda h: h["ccn"])}
    json.dump(gindex, open(os.path.join(prices_dir, "index.json"), "w"))
    log(f"prices/index.json: {n_ccns} hospitals")

    # ------------------------------------------------ 4. rebuild meta/ via build_site_data.py (DB-driven, reads merged index)
    pub_data = os.path.join(PUB, "public", "data")
    os.makedirs(os.path.join(pub_data, "prices"), exist_ok=True)
    shutil.copy2(os.path.join(prices_dir, "index.json"),
                 os.path.join(pub_data, "prices", "index.json"))
    scripts_dir = os.path.join(PUB, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    shutil.copy2(os.path.join(REPO, "scripts", "build_site_data.py"), scripts_dir)
    db_link = os.path.join(PUB, "db")
    if not os.path.exists(db_link):
        os.symlink(os.path.join(REPO, "db"), db_link)
    log("running build_site_data.py")
    r = subprocess.run([sys.executable, os.path.join(scripts_dir, "build_site_data.py")],
                       capture_output=True, text=True, timeout=3600, cwd=PUB)
    if r.returncode != 0:
        fail(f"build_site_data.py failed: {r.stderr[-2000:]}")
    log(f"build_site_data.py ok: {(r.stdout or '').strip().splitlines()[-1] if (r.stdout or '').strip() else ''}")

    # ------------------------------------------------ 5. assemble publish layout (== R2 root layout)
    meta_dir = os.path.join(PUB, "meta")
    os.makedirs(meta_dir, exist_ok=True)
    for n in ("hospitals.json", "summary.json"):
        shutil.copy2(os.path.join(pub_data, n), os.path.join(meta_dir, n))
    idx_dir = os.path.join(PUB, "indexes")
    os.makedirs(idx_dir, exist_ok=True)
    shutil.copy2(os.path.join(MERGE_DIR, "indexes", "cpt-index.json"),
                 os.path.join(idx_dir, "cpt-index.json"))
    agg_dir = os.path.join(PUB, "aggregates")
    if os.path.isdir(agg_dir):
        shutil.rmtree(agg_dir)
    shutil.copytree(os.path.join(MERGE_DIR, "aggregates"), agg_dir)
    log("publish layout assembled")

    # ------------------------------------------------ 6. manifest (before validation, like the merge)
    summary = json.load(open(os.path.join(meta_dir, "summary.json")))
    gen_at = summary["generated_at"]
    artifacts = {}
    for root, _d, files in os.walk(PUB):
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, PUB)
            if rel == "meta/manifest.json":
                continue
            if not rel.startswith(PUBLISH_PREFIXES):
                continue
            artifacts[rel] = {"bytes": os.path.getsize(fp), "sha256": sha256_file(fp)}
    manifest = {"contract_version": CONTRACT_VERSION, "generated_at": gen_at,
                "producer": PRODUCER, "refresh_tier": "full",
                "gen_id": GEN_ID,
                "parent_gen": "gen/gen-20260923-vm/",
                "carried_forward_live_ccns": live_only,
                "aggregate_coverage_note": (
                    "aggregates/ and indexes/cpt-index.json are the validated Tier-3 "
                    "build outputs (3,699-hospital corpus). The carried-forward live "
                    "CCNs are present in prices/, prices/index.json, and meta/."),
                "artifacts": artifacts}
    mp = os.path.join(meta_dir, "manifest.json")
    json.dump(manifest, open(mp, "w"), indent=1)
    log(f"manifest: {len(artifacts)} artifacts, gen_id={GEN_ID}")

    # ------------------------------------------------ 7. validate --tier full (MUST PASS)
    log("running validate_site_data.py --tier full")
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "validate_site_data.py"),
                        "--tier", "full", PUB],
                       capture_output=True, text=True, timeout=7200, cwd=REPO)
    log(f"validator exit={r.returncode}")
    log(f"validator tail:\n{(r.stdout or '')[-2000:]}")
    if r.returncode != 0:
        fail(f"validate_site_data.py --tier full FAILED:\n{(r.stdout or '')[-3000:]}\n{(r.stderr or '')[-1000:]}")
    log("VALIDATION PASS --tier full")


def _publish_all(client, artifacts, manifest_path):
    mp = manifest_path
    # ------------------------------------------------ 8. snapshot current live root (rollback record)
    os.makedirs(ROLLBACK_DIR, exist_ok=True)
    for key in ("meta/manifest.json", "prices/index.json", "meta/summary.json", "meta/hospitals.json"):
        dst = os.path.join(ROLLBACK_DIR, key.replace("/", "_"))
        ok, _, _ = client.get_file(key, dst)
        log(f"rollback snapshot {key}: {'ok' if ok else 'MISS'}")

    # ------------------------------------------------ 9. publish to root, manifest LAST
    # parallel verify-first upload (R2Client is thread-safe: one persistent
    # connection per thread). Progress file is the resume authority.
    import threading as _threading
    plock = _threading.Lock()
    progress = {}
    if os.path.isfile(PROGRESS):
        try:
            progress = json.load(open(PROGRESS))
        except Exception:  # noqa: BLE001
            progress = {}

    def save_progress():
        with plock:
            tmp = PROGRESS + ".tmp"
            json.dump(progress, open(tmp, "w"))
            os.replace(tmp, PROGRESS)

    confirmed = [0]
    staged = []
    failures = []

    def put_one(local, key, expected_sha):
        with plock:
            rec = progress.get(key)
        if rec and rec.get("sha256") == expected_sha:
            with plock:
                confirmed[0] += 1
                n = confirmed[0]
            if n % 2000 == 0:
                log(f"publish progress: {n}/{len(artifacts)} confirmed")
            return "cached"
        size = os.path.getsize(local)
        try:
            vok, vinfo = client.get_sha256(key, expect_len=size)
        except Exception:  # noqa: BLE001
            vok, vinfo = False, "get-raised"
        if vok and vinfo == expected_sha:
            with plock:
                progress[key] = {"bytes": size, "sha256": expected_sha}
                confirmed[0] += 1
                n = confirmed[0]
            save_progress()
            if n % 2000 == 0:
                log(f"publish progress: {n}/{len(artifacts)} confirmed")
            return "skipped"
        ok, info = client.put_verified(local, key)
        if not ok:
            with plock:
                failures.append((key, info))
            return "FAILED"
        with plock:
            progress[key] = {"bytes": size, "sha256": info}
            staged.append(key)
            confirmed[0] += 1
            n = confirmed[0]
        save_progress()
        if n % 2000 == 0:
            log(f"publish progress: {n}/{len(artifacts)} confirmed")
        return "staged"

    keys = [rel for rel in sorted(artifacts) if rel != "meta/manifest.json"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda rel: put_one(os.path.join(PUB, rel), rel,
                                       artifacts[rel]["sha256"]), keys))
    if failures:
        fail(f"publish failed for {len(failures)} keys: {failures[:5]}")
    log(f"publish pass done: {len(staged)} uploaded this run, {confirmed[0]} confirmed total")
    put_one(mp, "meta/manifest.json", sha256_file(mp))
    log("published meta/manifest.json LAST")

    # ------------------------------------------------ 10. spot-check 3 random staged CCNs at root
    manifest = json.load(open(manifest_path))
    price_ccns = sorted(a[7:-5] for a in artifacts
                        if a.startswith("prices/") and a.endswith(".json")
                        and a != "prices/index.json")
    import random
    random.seed(20260926)
    for ccn in random.sample(price_ccns, 3):
        lp = os.path.join(PUB, "prices", f"{ccn}.json")
        ok, h = client.get_sha256(f"prices/{ccn}.json", expect_len=os.path.getsize(lp))
        if not ok:
            fail(f"spot-check GET failed for {ccn}: {h}")
        if h != sha256_file(lp):
            fail(f"spot-check hash mismatch for {ccn}")
        log(f"spot-check {ccn}: root sha256 matches local")

    record = {"verdict": "pass", "finished_at": utcnow(),
              "gen_id": GEN_ID, "n_ccns": len(price_ccns),
              "n_price_files": len(price_ccns),
              "carried_forward_live_ccns": len(manifest.get("carried_forward_live_ccns", [])),
              "artifacts_published": len(artifacts),
              "uploaded_this_run": len(staged)}
    json.dump(record, open(os.path.join(PUB, "publish-record.json"), "w"), indent=1)
    log(f"PUBLISH COMPLETE: {json.dumps(record)}")


if __name__ == "__main__":
    main()
