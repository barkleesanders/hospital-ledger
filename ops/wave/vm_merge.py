#!/usr/bin/env python3
"""VM rebuild merge worker: waves -> global generation, validated, staged.

Reads per-wave outputs (local keep cache + R2), merges to the global
generation layout, runs build_site_data.py + build_aggregates.py,
validates --tier full, stages to R2, and writes the merge report.

Final layout (validated locally, then uploaded under PREFIX):
  meta/manifest.json, meta/summary.json, meta/hospitals.json
  prices/index.json, prices/{ccn}.json
  indexes/cpt-index.json
  aggregates/payers-index.json, aggregates/compliance-ranking.json
  aggregates/cpt-detail/{code}.json, aggregates/payer/{slug}.json
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HF = HERE  # canonical progress log lives beside the wave plan
sys.path.insert(0, HERE)
REPO = os.environ.get("HL_REPO", os.path.expanduser("~/hospital-ledger"))
WAVES_DIR = os.path.join(os.path.expanduser("~"), "hl-vm-waves")
MERGE_DIR = os.path.join(WAVES_DIR, "merge")
RESULTS_DIR = os.path.join(WAVES_DIR, "results")
PREFIX = "gen/gen-20260923-vm/"
CONTRACT_VERSION = "1"

EXPECTED_WAVES = list(range(1, 12))
CPT_BUCKETS = 256
CPT_DETAIL_KEEP = 10000
CPT_INDEX_KEEP = 5000
CPT_INDEX_MAX_BYTES = 15 * 1024 * 1024


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(msg):
    line = f"[{utcnow()}] [merge] {msg}"
    print(line, flush=True)
    with open(os.path.join(HF, "vm-rebuild-progress.log"), "a") as f:
        f.write(line + "\n")


def fail(msg):
    log(f"MERGE FAIL: {msg}")
    json.dump({"verdict": "fail", "error": msg, "finished_at": utcnow()},
              open(os.path.join(RESULTS_DIR, "merge.json"), "w"), indent=1)
    sys.exit(1)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cpt_index_entry_sort_key(entry):
    gross = entry.get('gross')
    if gross is None:
        return (1, 0.0)
    try:
        return (0, -float(gross))
    except (TypeError, ValueError):
        return (0, 0.0)


def _trim_cpt_index_entry(entry):
    out = {}
    for k, v in entry.items():
        if k in ('payers_top5', 'desc'):
            continue
        if v is None:
            continue
        if k == 'pc' and v == 0:
            continue
        out[k] = v
    return out


def merge_cpt_detail(wave_detail_files, out_detail_path, out_index_path, tmp_dir):
    """Merge per-wave _cpt_detail_raw.jsonl into exact global top-N artifacts.

    Returns (coverage_map, index_dict, detail_count, bound5k, bound10k).
    Exactness: every code with coverage above the rank-5000 boundary is in
    the index; verified, not assumed.
    """
    bucket_dir = os.path.join(tmp_dir, "cpt_buckets")
    os.makedirs(bucket_dir, exist_ok=True)
    n_recs = 0
    for fp in wave_detail_files:
        bhs = [open(os.path.join(bucket_dir, f"b{i:03d}.jsonl"), "a")
               for i in range(CPT_BUCKETS)]
        with open(fp) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                code = json.loads(line)["code"]
                bhs[hash(code) % CPT_BUCKETS].write(line + "\n")
                n_recs += 1
        for bh in bhs:
            bh.close()
    coverage = {}
    for i in range(CPT_BUCKETS):
        bp = os.path.join(bucket_dir, f"b{i:03d}.jsonl")
        groups = {}
        order = []
        with open(bp) as f:
            for line in f:
                rec = json.loads(line)
                code = rec["code"]
                if code not in groups:
                    groups[code] = []
                    order.append(code)
                groups[code].extend(rec["entries"])
        gp = os.path.join(bucket_dir, f"g{i:03d}.tsv")
        with open(gp, "w") as out:
            for code in order:
                ents = groups[code]
                coverage[code] = (len(ents), i)
                out.write(code + "\t" + json.dumps(ents, separators=(",", ":")) + "\n")
        os.remove(bp)
    ranked = sorted(coverage.items(), key=lambda kv: (-kv[1][0], kv[0]))
    rank_of = {code: r for r, (code, _) in enumerate(ranked)}
    top5k = set(code for code, _ in ranked[:CPT_INDEX_KEEP])
    bound5k = ranked[CPT_INDEX_KEEP - 1][1][0] if len(ranked) >= CPT_INDEX_KEEP else 0
    bound10k = ranked[CPT_DETAIL_KEEP - 1][1][0] if len(ranked) >= CPT_DETAIL_KEEP else 0
    viol = [c for c, (cov, _) in coverage.items()
            if c not in top5k and cov > bound5k]
    if viol:
        raise RuntimeError(f"CPT exactness violated: {len(viol)} codes exceed boundary")
    rank_tmp = os.path.join(tmp_dir, "cpt_rank")
    os.makedirs(rank_tmp, exist_ok=True)
    index = {}
    detail_n = 0
    for i in range(CPT_BUCKETS):
        gp = os.path.join(bucket_dir, f"g{i:03d}.tsv")
        with open(gp) as f:
            for line in f:
                code, ents_j = line.rstrip("\n").split("\t", 1)
                r = rank_of.get(code)
                if r is None or r >= CPT_DETAIL_KEEP:
                    continue
                entries = json.loads(ents_j)
                with open(os.path.join(rank_tmp, f"{r:06d}.json"), "w") as out:
                    out.write(json.dumps({"code": code, "entries": entries},
                                         separators=(",", ":")) + "\n")
                detail_n += 1
                if r < CPT_INDEX_KEEP:
                    capped = sorted(entries, key=_cpt_index_entry_sort_key)[:25]
                    index[code] = [_trim_cpt_index_entry(e) for e in capped]
        os.remove(gp)
    if len(ranked) >= CPT_INDEX_KEEP and len(index) != CPT_INDEX_KEEP:
        raise RuntimeError(f"cpt-index has {len(index)} codes, expected {CPT_INDEX_KEEP}")
    os.makedirs(os.path.dirname(out_index_path), exist_ok=True)
    with open(out_index_path, "w") as f:
        json.dump(index, f, separators=(",", ":"))
    if os.path.getsize(out_index_path) > CPT_INDEX_MAX_BYTES:
        raise RuntimeError("cpt-index.json exceeds 15 MB cap")
    os.makedirs(os.path.dirname(out_detail_path), exist_ok=True)
    with open(out_detail_path, "w") as out:
        for r in range(min(CPT_DETAIL_KEEP, len(ranked))):
            p = os.path.join(rank_tmp, f"{r:06d}.json")
            if os.path.isfile(p):
                out.write(open(p).read())
    shutil.rmtree(bucket_dir, ignore_errors=True)
    shutil.rmtree(rank_tmp, ignore_errors=True)
    return coverage, index, detail_n, bound5k, bound10k


def main():
    t0 = time.time()
    os.makedirs(MERGE_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    log("merge start")

    # ------------------------------------------------ 1. wave inputs
    keep = {}
    for w in EXPECTED_WAVES:
        kd = os.path.join(WAVES_DIR, "keep", f"w{w}")
        if not os.path.isdir(kd):
            fail(f"keep dir missing for wave {w}: {kd}")
        rj = os.path.join(RESULTS_DIR, f"w{w}.json")
        if not os.path.isfile(rj):
            fail(f"wave result missing for wave {w}")
        r = json.load(open(rj))
        if r.get("verdict") != "pass":
            fail(f"wave {w} verdict is {r.get('verdict')}, not pass")
        keep[w] = kd
    log(f"all {len(keep)} wave keep dirs present, all verdicts pass")

    # ------------------------------------------------ 2. merge prices/index.json
    # The wave slim index is a CCN-keyed dict; the PUBLISHED prices/index.json
    # is {"hospitals": [...]} (see patch_index_from_prices.py + the live file).
    # We build it authoritatively from the staged per-CCN price files, sorted
    # by CCN — the same summary_from_prices_file() the pipeline uses.
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from patch_index_from_prices import summary_from_prices_file  # noqa: E402
    from pathlib import Path  # noqa: E402
    prices_dir = os.path.join(MERGE_DIR, "prices")
    os.makedirs(prices_dir, exist_ok=True)
    gindex_ccns = set()
    n_price_files = 0
    for w in EXPECTED_WAVES:
        wpi = os.path.join(keep[w], "public", "data", "prices", "index.json")
        d = json.load(open(wpi))
        # wave index is {"hospitals": [...]} (slim_parsed.py output shape)
        for h in d.get("hospitals", []):
            ccn = h.get("ccn")
            if ccn in gindex_ccns:
                fail(f"duplicate CCN {ccn} in wave {w} index")
            gindex_ccns.add(ccn)
        # copy price files (already local in keep cache)
        wprices = os.path.join(keep[w], "public", "data", "prices")
        for fn in os.listdir(wprices):
            if fn == "index.json" or not fn.endswith(".json"):
                continue
            shutil.copy2(os.path.join(wprices, fn), os.path.join(prices_dir, fn))
            n_price_files += 1
    log(f"wave indexes merged: {len(gindex_ccns)} CCNs, {n_price_files} price files copied")
    if len(gindex_ccns) != 3699:
        fail(f"global price index has {len(gindex_ccns)} CCNs, expected 3699")
    if n_price_files != 3699:
        fail(f"copied {n_price_files} price files, expected 3699")
    hospitals = []
    for fn in sorted(os.listdir(prices_dir)):
        if fn == "index.json" or not fn.endswith(".json"):
            continue
        s = summary_from_prices_file(Path(prices_dir) / fn)
        if not s:
            fail(f"summary_from_prices_file failed for {fn}")
        hospitals.append(s)
    if len(hospitals) != 3699:
        fail(f"built {len(hospitals)} hospital summaries, expected 3699")
    gindex = {"hospitals": sorted(hospitals, key=lambda h: h["ccn"])}
    json.dump(gindex, open(os.path.join(prices_dir, "index.json"), "w"))
    log(f"prices/index.json: {len(hospitals)} hospitals, "
        f"{os.path.getsize(os.path.join(prices_dir, 'index.json')) / 1e6:.1f} MB")

    # ------------------------------------------------ 3. merge CPT detail (exact global top-N)
    data_dir = os.path.join(MERGE_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    log("CPT merge: bucket -> group -> rank -> emit")
    wave_detail_files = []
    for w in EXPECTED_WAVES:
        fp = os.path.join(keep[w], "data", "_cpt_detail_raw.jsonl")
        if not os.path.isfile(fp):
            fail(f"wave {w} missing _cpt_detail_raw.jsonl")
        wave_detail_files.append(fp)
    idx_path = os.path.join(MERGE_DIR, "indexes", "cpt-index.json")
    try:
        coverage, index, detail_n, bound5k, bound10k = merge_cpt_detail(
            wave_detail_files,
            os.path.join(data_dir, "_cpt_detail_raw.jsonl"),
            idx_path,
            os.path.join(MERGE_DIR, "tmp"))
    except RuntimeError as e:
        fail(str(e))
    idx_bytes = os.path.getsize(idx_path)
    log(f"CPT merge done: {len(coverage)} codes; index {len(index)} codes, "
        f"{idx_bytes / 1048576:.2f} MB; top5000 boundary={bound5k}; "
        f"top10000 boundary={bound10k}; detail={detail_n} codes; exactness holds")
    shutil.rmtree(os.path.join(MERGE_DIR, "tmp"), ignore_errors=True)

    # ------------------------------------------------ 4. payer + compliance concat
    for name in ("_payer_raw.jsonl", "_compliance_per_hospital.jsonl"):
        with open(os.path.join(data_dir, name), "w") as out:
            for w in EXPECTED_WAVES:
                fp = os.path.join(keep[w], "data", name)
                if not os.path.isfile(fp):
                    fail(f"wave {w} missing {name}")
                with open(fp) as f:
                    shutil.copyfileobj(f, out)
        log(f"merged {name}: {os.path.getsize(os.path.join(data_dir, name)) / 1e6:.1f} MB")

    # ------------------------------------------------ 5. site data + aggregates
    pub = os.path.join(MERGE_DIR, "public", "data")
    os.makedirs(pub, exist_ok=True)
    os.makedirs(os.path.join(pub, "prices"), exist_ok=True)
    shutil.copy2(os.path.join(prices_dir, "index.json"), os.path.join(pub, "prices", "index.json"))
    scripts_dir = os.path.join(MERGE_DIR, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    for sn in ("build_site_data.py", "build_aggregates.py", "payer_canonical.py"):
        shutil.copy2(os.path.join(REPO, "scripts", sn), os.path.join(scripts_dir, sn))
    db_link = os.path.join(MERGE_DIR, "db")
    if not os.path.exists(db_link):
        os.symlink(os.path.join(REPO, "db"), db_link)
    log("running build_site_data.py")
    r = subprocess.run([sys.executable, os.path.join(scripts_dir, "build_site_data.py")],
                       capture_output=True, text=True, timeout=3600, cwd=MERGE_DIR)
    if r.returncode != 0:
        fail(f"build_site_data.py failed: {r.stderr[-2000:]}")
    log(f"build_site_data.py ok: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''}")
    log("running build_aggregates.py")
    r = subprocess.run([sys.executable, os.path.join(scripts_dir, "build_aggregates.py")],
                       capture_output=True, text=True, timeout=7200, cwd=MERGE_DIR)
    if r.returncode != 0:
        fail(f"build_aggregates.py failed: {r.stderr[-2000:]}")
    log(f"build_aggregates.py ok:\n{r.stdout.strip()[-1500:]}")

    # ------------------------------------------------ 6. assemble validator layout
    meta_dir = os.path.join(MERGE_DIR, "meta")
    os.makedirs(meta_dir, exist_ok=True)
    for n in ("hospitals.json", "summary.json"):
        shutil.copy2(os.path.join(pub, n), os.path.join(meta_dir, n))
    agg_dir = os.path.join(MERGE_DIR, "aggregates")
    os.makedirs(agg_dir, exist_ok=True)
    for n in ("compliance-ranking.json", "payers-index.json"):
        shutil.copy2(os.path.join(pub, n), os.path.join(agg_dir, n))
    for n in ("cpt-detail", "payer"):
        src = os.path.join(pub, n)
        dst = os.path.join(agg_dir, n)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
    idx_final = os.path.join(MERGE_DIR, "indexes", "cpt-index.json")
    log("layout assembled")

    # ------------------------------------------------ 7. validate
    log("running validate_site_data.py --tier full")
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "validate_site_data.py"),
                        "--tier", "full", MERGE_DIR],
                       capture_output=True, text=True, timeout=7200, cwd=REPO)
    log(f"validator exit={r.returncode}")
    tail = (r.stdout or "")[-4000:]
    log(f"validator tail:\n{tail}")
    if r.returncode != 0:
        fail(f"validate_site_data.py --tier full FAILED:\n{(r.stdout or '')[-3000:]}\n{(r.stderr or '')[-1000:]}")
    log("VALIDATION PASS --tier full")

    # ------------------------------------------------ 8. manifest (last local write)
    summary = json.load(open(os.path.join(meta_dir, "summary.json")))
    gen_at = summary["generated_at"]
    artifacts = {}
    for root, _d, files in os.walk(MERGE_DIR):
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, MERGE_DIR)
            if not rel.startswith(("meta/", "prices/", "indexes/", "aggregates/")):
                continue  # build tree / intermediates are not published
            artifacts[rel] = {"bytes": os.path.getsize(fp), "sha256": sha256_file(fp)}
    manifest = {"contract_version": CONTRACT_VERSION, "generated_at": gen_at,
                "producer": "hl-vm-rebuild", "refresh_tier": "full",
                "gen_id": "gen-20260923-vm", "artifacts": artifacts}
    mp = os.path.join(meta_dir, "manifest.json")
    json.dump(manifest, open(mp, "w"), indent=1)
    artifacts["meta/manifest.json"] = {"bytes": os.path.getsize(mp), "sha256": sha256_file(mp)}
    json.dump(manifest, open(mp, "w"), indent=1)
    log(f"manifest: {len(artifacts)} artifacts")

    # ------------------------------------------------ 9. stage to R2 (manifest last)
    from vm_r2client import R2Client
    client = R2Client(workers=8)
    staged = []

    def put_one(local, key):
        ok, info = client.put_verified(local, key)
        if not ok:
            fail(f"merge staging failed for {key}: {info}")
        staged.append({"key": key, "bytes": os.path.getsize(local), "sha256": info})

    # prices/{ccn}.json already staged per-wave; stage everything else, manifest last
    for rel, meta in sorted(artifacts.items()):
        if rel.startswith("prices/") and rel != "prices/index.json":
            continue
        if rel == "meta/manifest.json":
            continue
        put_one(os.path.join(MERGE_DIR, rel), PREFIX + rel)
    log(f"staged {len(staged)} non-manifest artifacts")
    put_one(mp, PREFIX + "meta/manifest.json")
    log("staged meta/manifest.json LAST")

    # ------------------------------------------------ 10. spot-check 3 random staged CCNs
    import random
    random.seed(20260923)
    ccns = random.sample(sorted(h["ccn"] for h in gindex["hospitals"]), 3)
    for ccn in ccns:
        lp = os.path.join(prices_dir, f"{ccn}.json")
        h = client.sha256(PREFIX + f"prices/{ccn}.json")
        if h != sha256_file(lp):
            fail(f"spot-check hash mismatch for {ccn}")
        log(f"spot-check {ccn}: staged sha256 matches local")

    elapsed = time.time() - t0
    report = {"verdict": "pass", "finished_at": utcnow(),
              "elapsed_s": round(elapsed, 1),
              "n_ccns": len(gindex["hospitals"]),
              "n_price_files": n_price_files,
              "cpt_codes_total": len(coverage),
              "cpt_index_codes": len(index),
              "cpt_index_bytes": idx_bytes,
              "cpt_top5000_boundary_coverage": bound5k,
              "cpt_top10000_boundary_coverage": bound10k,
              "cpt_detail_codes": detail_n,
              "staged": len(staged),
              "spot_checked": ccns}
    json.dump(report, open(os.path.join(RESULTS_DIR, "merge.json"), "w"), indent=1)
    n_hosp = len(gindex["hospitals"])
    log(f"MERGE PASS: {n_hosp} CCNs, {len(staged)} staged, "
        f"{elapsed / 3600:.2f}h elapsed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
