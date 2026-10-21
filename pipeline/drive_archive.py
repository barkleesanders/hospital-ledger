#!/usr/bin/env python3
"""Build Hospital Ledger parsed/ cold archive: R2 -> gzip shards -> Google Drive.

Read-only against R2. No R2 writes, no R2 deletes. Uploads go to the Mac mini
via scp, then to Drive with gog. One shard at a time on local disk.

Mini-leg mode (HL_ON_MINI=1): run the whole driver ON the Mac mini — R2
downloads use the mini's egress and Drive uploads use local gog with no SSH
hops. HL_HIDDEN/HL_ENV_PATH/HL_R2PUT_DIR override the VM paths;
HL_SHARD_TARGET_MB keeps staging inside a small disk; HL_WORKERS caps
download parallelism.
"""
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
# Mini-leg mode (2026-09-23): run the whole driver on the Mac mini so R2
# downloads use the mini's egress and Drive uploads use local gog (no SSH).
# Enable with HL_ON_MINI=1. Orchestration-owned knobs; scripts/*.py untouched.
ON_MINI = os.environ.get("HL_ON_MINI") == "1"
HIDDEN = Path(os.environ.get("HL_HIDDEN", str(
    HOME / "workspace/goals/hospital-ledger-weekly-refresh-counts-tier/hidden_files")))
R2PUT_DIR = os.environ.get("HL_R2PUT_DIR", str(HOME / "hospital-ledger/scripts"))
sys.path.insert(0, R2PUT_DIR)
from r2_put import _signing_key, REGION, SERVICE  # noqa: E402  (crypto only; NOT its urllib transport)

FOLDER_ID = "1dlnxbZqu82_LchT7HiR8IDZP2Z3BCayw"
GOG_ACCT = "barkleesanders@gmail.com"
SSH_CFG = "/home/hatch/.ssh/mini_ssh_config"
WORK = HIDDEN / "archive-run"
HEARTBEAT = WORK / "heartbeat.json"
STAGE = WORK / "stage"
SHARD_DIR = WORK / "shards"
KEYLIST = WORK / "parsed_keys.tsv"
LOG = HIDDEN / "drive_archive_log.jsonl"
MANIFEST = HIDDEN / "drive_archive_manifest.json"
ENV_PATH = Path(os.environ.get("HL_ENV_PATH", str(HOME / "hospital-ledger.env")))
EST_RATIO = 0.06          # conservative vs measured 0.05
# Shard target in MB of compressed bytes; env-overridable for small disks
# (Mac mini / has ~4 GB free -> HL_SHARD_TARGET_MB=120 keeps stage ~2 GB).
SHARD_TARGET = int(os.environ.get("HL_SHARD_TARGET_MB", "450")) * 1024 * 1024
MAX_TOTAL = 14 * 1024**3  # hard stop: free Drive tier overflow
DL_WORKERS = int(os.environ.get("HL_WORKERS", "8"))

if ON_MINI:
    # homebrew gog/python live outside the ssh non-interactive PATH
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def write_heartbeat(shard, status):
    """Watcher cron uses this for liveness; never lives in /tmp."""
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps(
            {"ts": int(time.time()), "shard": shard, "status": status}))
    except Exception as e:  # noqa: BLE001
        print(f"heartbeat write failed: {e}", flush=True)


def load_env():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


# ---------------------------------------------------------------------------
# R2 transport via curl (NOT urllib).
# Measured 2026-09-23: Python urllib through the VM egress proxy silently
# truncates / raises IncompleteRead on R2 responses larger than ~2 KB, while
# curl through the same proxy downloads 15 MB objects byte-perfect 3/3.
# curl --fail exits non-zero on short transfers, so failures are loud.
# ---------------------------------------------------------------------------

def _r2_creds():
    return (os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"],
            os.environ["R2_ENDPOINT"].rstrip("/"),
            os.environ.get("R2_BUCKET", "hl-mrf-parsed"))


def _sign_curl(method, key, query_params=()):
    """Return (url, curl -H args) for a SigV4-signed R2 request."""
    ak, sk, endpoint, bucket = _r2_creds()
    host = urllib.parse.urlparse(endpoint).netloc
    path = "/" + bucket + "/" + (urllib.parse.quote(key, safe="/-_.~") if key else "")
    params = sorted(query_params, key=lambda p: p[0])
    parts = []
    for name, val in params:
        # R2 returns continuation tokens already percent-encoded EXCEPT for
        # the trailing base64 '=' padding. quote(..., safe="%-_.~") preserves
        # existing %XX sequences while encoding the stray '=' -> %3D, exactly
        # matching r2_put.py's working list_keys signing. Verbatim splicing
        # here caused SignatureDoesNotMatch on every page 2+ (2026-09-23).
        if name == "continuation-token":
            parts.append("continuation-token="
                         + urllib.parse.quote(val, safe="%-_.~"))
        else:
            parts.append(urllib.parse.urlencode([(name, val)]))
    query = "&".join(parts)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = "\n".join([method, path, query, canonical_headers, signed, payload_hash])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}")
    url = endpoint + path + ("?" + query if query else "")
    args = []
    for k, v in headers.items():
        if k != "host":
            args += ["-H", f"{k}: {v}"]
    return url, args


def _xml_text(parent, tag):
    el = parent.find(f"{{http://s3.amazonaws.com/doc/2006-03-01/}}{tag}")
    if el is None:
        el = parent.find(tag)
    return el.text if el is not None else None


def r2_list_curl(prefix):
    """Paginated ListObjectsV2 via curl. Returns [(key, size)]. Per-page retry."""
    keys = []
    token = None
    while True:
        params = [("list-type", "2"), ("max-keys", "1000"), ("prefix", prefix)]
        if token:
            params.append(("continuation-token", token))
        url, hargs = _sign_curl("GET", "", params)
        last_err = ""
        page = None
        for attempt in range(1, 7):
            r = subprocess.run(["curl", "-sS", "--fail", "-m", "120"] + hargs + [url],
                               capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip().startswith("<"):
                page = r.stdout
                break
            last_err = (r.stderr or "")[-200:] or f"exit={r.returncode}"
            time.sleep(min(2 ** attempt, 30))
        if page is None:
            raise RuntimeError(f"R2 list page failed after 6 attempts: {last_err}")
        root = ET.fromstring(page)
        for child in root:
            tag = child.tag.split("}", 1)[-1]
            if tag != "Contents":
                continue
            k = _xml_text(child, "Key")
            s = _xml_text(child, "Size")
            if k is not None and s is not None:
                keys.append((k, int(s)))
        is_trunc = (_xml_text(root, "IsTruncated") or "").strip().lower() == "true"
        if is_trunc:
            token = (_xml_text(root, "NextContinuationToken") or "").strip()
            if not token:
                raise RuntimeError("IsTruncated=true but no NextContinuationToken")
        else:
            break
    return keys


def r2_get_curl(key, dest, expect_size):
    """Download one R2 object via curl. True only if size matches exactly."""
    url, hargs = _sign_curl("GET", key)
    try:
        if dest.exists():
            dest.unlink()
        r = subprocess.run(
            ["curl", "-sS", "--fail", "-m", "600", "-o", str(dest)] + hargs + [url],
            capture_output=True, text=True, timeout=660)
        if r.returncode != 0:
            return False
        return dest.exists() and dest.stat().st_size == expect_size
    except Exception:  # noqa: BLE001
        return False


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def get_keys():
    KEYLIST.parent.mkdir(parents=True, exist_ok=True)
    if not KEYLIST.exists():
        log("listing parsed/ from R2 (curl transport)...")
        keys = None
        last = ""
        for full_attempt in range(1, 4):
            try:
                keys = r2_list_curl("parsed/")
                break
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                log(f"  full listing attempt {full_attempt} failed: {last[:160]}; "
                    f"retrying in {10 * full_attempt}s")
                time.sleep(10 * full_attempt)
        if keys is None:
            raise RuntimeError(f"R2 parsed/ listing failed after 3 full attempts: {last}")
        with open(KEYLIST, "w") as f:
            for key, size in keys:
                f.write(f"{size}\t{key}\n")
        log(f"key list cached: {len(keys)} keys")
    keys = []
    seen = set()
    for line in KEYLIST.read_text().splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        size_s, key = line.split("\t", 1)
        if key in seen:
            continue
        seen.add(key)
        keys.append((key, int(size_s)))
    keys.sort()
    if len(keys) != 3699:
        log(f"WARNING: expected 3699 parsed keys, got {len(keys)}")
    return keys


def plan_shards(keys):
    shards, cur, cur_raw = [], [], 0
    for key, size in keys:
        cur.append((key, size))
        cur_raw += size
        if cur_raw * EST_RATIO >= SHARD_TARGET:
            shards.append(cur)
            cur, cur_raw = [], 0
    if cur:
        shards.append(cur)
    return shards


def download_one(args):
    key, size, dest = args
    for attempt in (1, 2, 3, 4):
        try:
            if r2_get_curl(key, dest, size):
                return (key, True, sha256_file(dest), "")
            err = (f"size mismatch (got {dest.stat().st_size if dest.exists() else -1}, "
                   f"want {size})")
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        time.sleep(min(2 ** attempt, 20))
    return (key, False, "", err)


def mini(cmd, timeout=120):
    if ON_MINI:
        # already on the mini: run locally, no SSH hop
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return r
    r = subprocess.run(["ssh", "-F", SSH_CFG, "-o", "ConnectTimeout=20", "mini", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r


def scp_to_mini(local, timeout=600):
    if ON_MINI:
        # local copy into the staging dir gog uploads from
        dest_dir = Path("/tmp/hl-shards")
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(local), dest_dir / Path(local).name)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    mini("mkdir -p /tmp/hl-shards")
    r = subprocess.run(["scp", "-F", SSH_CFG, "-o", "ConnectTimeout=20",
                        str(local), "mini:/tmp/hl-shards/"],
                       capture_output=True, text=True, timeout=timeout)
    return r


def gog_upload(remote_name, timeout=900):
    r = mini(f"gog -a {GOG_ACCT} drive upload /tmp/hl-shards/{remote_name}"
             f" --parent {FOLDER_ID} --name {remote_name} -j --results-only", timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr[-300:]
    try:
        data = json.loads(r.stdout)
        fid = data.get("id") or data.get("fileId")
        return fid, ""
    except Exception as e:  # noqa: BLE001
        return None, f"json parse: {e}; stdout={r.stdout[:200]}"


def gog_meta(fid, timeout=120):
    r = mini(f"gog -a {GOG_ACCT} drive get {fid} -j --results-only"
             " --fields id,name,size,md5Checksum", timeout=timeout)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return None


def gog_delete(fid, timeout=120):
    r = mini(f"gog -a {GOG_ACCT} drive rm {fid} --force -j --results-only", timeout=timeout)
    return r.returncode == 0


def drive_listing(timeout=180):
    """Return {name: (file_id, size)} for the archive folder. Resume source."""
    r = mini(f"gog -a {GOG_ACCT} drive ls --parent {FOLDER_ID}"
             " -j --results-only --fields 'files(id,name,size)'", timeout=timeout)
    out = {}
    if r.returncode != 0:
        log(f"drive ls failed: {r.stderr[-200:]}")
        return out
    try:
        data = json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        log(f"drive ls json parse failed: {e}")
        return out
    items = data if isinstance(data, list) else data.get("files", data.get("items", []))
    for it in items:
        try:
            out[it["name"]] = (it.get("id") or it.get("fileId"), int(it.get("size", -1)))
        except Exception:  # noqa: BLE001
            continue
    return out


def already_archived():
    """Shard names recorded verified in the local log AND present in Drive."""
    done = {}
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("verified") and rec.get("shard"):
                done[rec["shard"]] = rec
    if not done:
        return {}
    in_drive = drive_listing()
    return {n: r for n, r in done.items()
            if n in in_drive and in_drive[n][1] == r.get("bytes")}


def append_log(rec):
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    load_env()
    WORK.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(exist_ok=True)
    SHARD_DIR.mkdir(exist_ok=True)
    write_heartbeat(0, "running")
    try:
        run()
    except Exception as e:  # noqa: BLE001
        log(f"FATAL: {type(e).__name__}: {e}")
        write_heartbeat(-1, "failed")
        raise


def run():
    keys = get_keys()
    log(f"{len(keys)} parsed keys, {sum(s for _, s in keys) / 1e9:.2f} GB raw")
    shards = plan_shards(keys)
    log(f"planned {len(shards)} shards")
    done = already_archived()
    if done:
        log(f"resume: {len(done)} shards already verified in Drive, skipping")

    manifest_shards = []
    total_compressed = 0
    for i, shard in enumerate(shards, 1):
        name = f"hl-parsed-shard-{i:03d}.tar.gz"
        if name in done:
            rec = done[name]
            total_compressed += rec["bytes"]
            manifest_shards.append({"shard": name,
                                    "drive_file_id": rec.get("drive_file_id"),
                                    "bytes": rec["bytes"], "sha256": rec["sha256"],
                                    "md5": rec.get("md5"), "verified": True,
                                    "files": [{"ccn": c} for c in rec.get("ccns", [])],
                                    "failed_files": rec.get("failed_files", []),
                                    "created_at": rec.get("created_at"),
                                    "resumed_from_log": True})
            log(f"shard {i}/{len(shards)}: {name} already archived, skipped")
            write_heartbeat(i, "running")
            continue
        # stage (resume-aware: a VM reboot / kill may have left a partial
        # stage behind. Keep files whose size already matches the expected
        # R2 size -- r2_get_curl only ever leaves exact-size files -- and
        # re-download only what is missing or partial. Drop anything that
        # does not belong to this shard.)
        STAGE.mkdir(exist_ok=True)
        log(f"shard {i}/{len(shards)}: {name} ({len(shard)} files, "
            f"{sum(s for _, s in shard) / 1e9:.2f} GB raw)")
        dests = [(k, s, STAGE / (Path(k).stem + ".json")) for k, s in shard]
        wanted = {d.name for _, _, d in dests}
        for p in STAGE.iterdir():
            if p.is_file() and p.name not in wanted:
                p.unlink()
        # download
        results = {}
        todo = []
        for key, size, dest in dests:
            if dest.exists() and dest.stat().st_size == size:
                # complete from a previous run; re-hash locally (cheap) so
                # the manifest carries a real per-file sha256
                results[key] = (True, sha256_file(dest), "")
            else:
                todo.append((key, size, dest))
        if results:
            log(f"  resume: {len(results)}/{len(dests)} files already complete, "
                f"{len(todo)} to download")
        n_done = len(results)
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            for key, ok, h, err in ex.map(download_one, todo):
                results[key] = (ok, h, err)
                n_done += 1
                if n_done % 25 == 0 or n_done == len(dests):
                    log(f"  download progress: {n_done}/{len(dests)} files")
                    write_heartbeat(i, "running")
        failed = [k for k, (ok, _, _) in results.items() if not ok]
        if failed:
            log(f"  WARNING: {len(failed)} files failed download, excluded from shard")
            for k in failed[:5]:
                log(f"    {k}: {results[k][2]}")
        ok_files = [(k, s, d) for (k, s, d) in dests if results[k][0]]
        # build tar.gz
        tarball = SHARD_DIR / name
        with tarfile.open(tarball, "w:gz", compresslevel=6) as tf:
            for k, s, d in ok_files:
                ti = tf.gettarinfo(str(d), arcname=d.name)
                ti.mtime = 0
                with open(d, "rb") as f:
                    tf.addfile(ti, f)
        tb_sha = sha256_file(tarball)
        tb_md5 = md5_file(tarball)
        tb_size = tarball.stat().st_size
        log(f"  compressed {tb_size / 1e6:.1f} MB sha256={tb_sha[:16]}...")
        if total_compressed + tb_size > MAX_TOTAL:
            log("  HARD STOP: would exceed 14 GB total. Aborting before upload.")
            append_log({"shard": name, "status": "aborted-size-cap",
                        "bytes": tb_size, "total_so_far": total_compressed})
            break
        # upload via mini
        fid, verified, up_err = None, False, ""
        r = scp_to_mini(tarball)
        if r.returncode == 0:
            for attempt in (1, 2):
                fid, up_err = gog_upload(name)
                if fid:
                    break
                log(f"  upload attempt {attempt} failed: {up_err}")
                time.sleep(5)
        else:
            up_err = f"scp failed: {r.stderr[-200:]}"
        if fid:
            meta = gog_meta(fid)
            if meta and int(meta.get("size", -1)) == tb_size and \
                    meta.get("md5Checksum") == tb_md5:
                verified = True
                mini(f"rm -f /tmp/hl-shards/{name}")
            else:
                up_err = f"verify mismatch: drive={meta} local_size={tb_size}"
        total_compressed += tb_size
        files_rec = [{"ccn": Path(k).stem, "key": k, "bytes": s,
                      "sha256": results[k][1]} for (k, s, d) in ok_files]
        rec = {"shard": name, "drive_file_id": fid, "bytes": tb_size,
               "sha256": tb_sha, "md5": tb_md5, "files": len(ok_files),
               "ccns": sorted(Path(k).stem for (k, s, d) in ok_files),
               "failed_files": failed, "verified": verified,
               "upload_error": up_err,
               "created_at": datetime.now(timezone.utc).isoformat()}
        append_log(rec)
        manifest_shards.append({"shard": name, "drive_file_id": fid,
                                "bytes": tb_size, "sha256": tb_sha, "md5": tb_md5,
                                "verified": verified, "files": files_rec,
                                "failed_files": failed,
                                "created_at": rec["created_at"]})
        log(f"  uploaded fid={fid} verified={verified}")
        tarball.unlink(missing_ok=True)
        shutil.rmtree(STAGE, ignore_errors=True)
        write_heartbeat(i, "running")

    # full CCN coverage sanity check before manifest
    planned_ccns = {Path(k).stem for shard in shards for k, _ in shard}
    logged_ccns = set()
    for s in manifest_shards:
        logged_ccns.update(f["ccn"] for f in s.get("files", []))
        logged_ccns.update(Path(k).stem for k in s.get("failed_files", []))
    missing_ccns = sorted(planned_ccns - logged_ccns)
    if missing_ccns:
        log(f"WARNING: {len(missing_ccns)} CCNs not covered by any shard: {missing_ccns[:10]}")
    else:
        log(f"coverage OK: all {len(planned_ccns)} CCNs represented in manifest")

    manifest = {"folder_id": FOLDER_ID,
                "folder_name": "HospitalLedger cold archives",
                "prefix": "parsed/", "shard_target_bytes": SHARD_TARGET,
                "total_compressed_bytes": total_compressed,
                "shard_count": len(manifest_shards),
                "all_verified": all(s["verified"] for s in manifest_shards),
                "ccn_count_planned": len(planned_ccns),
                "ccn_count_covered": len(logged_ccns),
                "missing_ccns": missing_ccns,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "shards": manifest_shards}
    MANIFEST.write_text(json.dumps(manifest, indent=1))
    log(f"manifest written: {len(manifest_shards)} shards, "
        f"{total_compressed / 1e9:.2f} GB total, all_verified={manifest['all_verified']}")
    # upload manifest + analysis doc to Drive
    for local in (MANIFEST, HIDDEN / "r2_prefix_bytes.json"):
        r = scp_to_mini(local)
        if r.returncode == 0:
            fid, err = gog_upload(local.name)
            log(f"doc {local.name}: fid={fid} err={err}")
            mini(f"rm -f /tmp/hl-shards/{local.name}")
    write_heartbeat(len(manifest_shards), "done")


if __name__ == "__main__":
    main()
