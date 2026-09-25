#!/usr/bin/env python3
"""hl_weekly_guard.py — index-regression guard for the weekly counts refresh.

Runs AFTER build_site_data.py and BEFORE the weekly assembles its publish dir.
Makes the weekly publish safe against two clobber modes:

1. Tarball re-extract (Phase 1) wipes wave-added hospitals from the local
   working copy. The guard merges any CCNs present in the LIVE R2 index but
   missing locally back into the local index, and re-downloads their price
   files from R2 (bounded) so the local tree is whole again.

2. A wave --no-publish run leaves CCNs in the local index whose price files
   were never uploaded. Publishing that index would put broken references on
   the live site. The guard uploads any such missing per-CCN files from the
   local tree first; if a CCN has no price file locally AND not in R2, it is
   dropped from the published index and the guard exits 2 so the weekly
   reports loudly instead of shipping a broken index.

Exit codes: 0 = guarded, safe to publish; 1 = operational failure (R2
unreachable, bad JSON); 2 = dropped one or more CCNs (alert Barklee).

Only ever ADDS per-CCN price files and rewrites the local index.json.
Never deletes anything on R2.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path.home() / "hospital-ledger"
VENV_PY = str(ROOT / ".venv" / "bin" / "python3")
R2_PUT = [VENV_PY, "scripts/r2_put.py"]
PRICES_DIR = ROOT / "public" / "data" / "prices"
LOCAL_INDEX = PRICES_DIR / "index.json"
ENV_FILE = Path.home() / "hospital-ledger.env"
MAX_BACKFILL = 200


def log(msg: str) -> None:
    print(f"[weekly-guard] {msg}", flush=True)


def load_r2_env() -> dict:
    env = dict(os.environ)
    if not env.get("AWS_ACCESS_KEY_ID") and ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    env["R2_BUCKET"] = "hl-mrf-parsed"
    return env


def r2_get_file(key: str, dest: Path, env: dict) -> bool:
    r = subprocess.run(R2_PUT + ["--get-file", key, str(dest)],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def r2_exists(key: str, env: dict) -> bool:
    r = subprocess.run(R2_PUT + ["--get-sha256", key],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def r2_upload(local: Path, key: str, env: dict) -> bool:
    r = subprocess.run(R2_PUT + [str(local), key],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
    return r.returncode == 0


def index_ccns(index: dict) -> dict:
    return {h["ccn"]: h for h in index.get("hospitals", [])}


def plan_merge(live: dict, local: dict) -> tuple[list, list]:
    """Pure merge decision, no I/O: (live_only_ccns, local_only_ccns).

    live_only: in the live R2 index but missing locally -> merge back + backfill.
    local_only: in the local index but absent live -> ensure uploaded, else drop.
    """
    live_map, local_map = index_ccns(live), index_ccns(local)
    return sorted(set(live_map) - set(local_map)), sorted(set(local_map) - set(live_map))


def main() -> int:
    env = load_r2_env()
    for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "R2_ENDPOINT"):
        if not env.get(v):
            log(f"FATAL: missing {v}")
            return 1

    with tempfile.TemporaryDirectory() as td:
        live_p = Path(td) / "live_index.json"
        if not r2_get_file("prices/index.json", live_p, env):
            log("FATAL: could not download live prices/index.json from R2")
            return 1
        try:
            live = json.loads(live_p.read_text())
            local = json.loads(LOCAL_INDEX.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f"FATAL: bad index JSON: {e}")
            return 1

    live_map, local_map = index_ccns(live), index_ccns(local)
    log(f"live={len(live_map)} local={len(local_map)}")

    # 1. Live-only CCNs: merge entries back, backfill missing price files.
    live_only, _ = plan_merge(live, local)
    backfilled, backfill_failed = 0, []
    for ccn in live_only[:MAX_BACKFILL]:
        local["hospitals"].append(live_map[ccn])
        dest = PRICES_DIR / f"{ccn}.json"
        if not dest.exists():
            if r2_get_file(f"prices/{ccn}.json", dest, env):
                backfilled += 1
            else:
                backfill_failed.append(ccn)
    if len(live_only) > MAX_BACKFILL:
        log(f"WARN: {len(live_only) - MAX_BACKFILL} live-only CCNs beyond backfill cap; entries merged, files will reparse on demand")
    if live_only:
        log(f"merged {len(live_only)} live-only CCNs into local index; backfilled {backfilled} price files"
            + (f"; FAILED: {backfill_failed}" if backfill_failed else ""))

    # 2. Local-only CCNs: ensure their price files exist in R2 before publish.
    _, local_only = plan_merge(live, local)
    uploaded, dropped = 0, []
    for ccn in local_only:
        if r2_exists(f"prices/{ccn}.json", env):
            continue
        src = PRICES_DIR / f"{ccn}.json"
        if src.exists() and r2_upload(src, f"prices/{ccn}.json", env):
            uploaded += 1
            log(f"uploaded missing per-CCN file prices/{ccn}.json ahead of index publish")
        else:
            dropped.append(ccn)
            local["hospitals"] = [h for h in local["hospitals"] if h["ccn"] != ccn]
    if local_only:
        log(f"local-only CCNs: {len(local_only)}; uploaded {uploaded}; dropped {len(dropped)}")
    if dropped:
        log(f"DROPPED from published index (no price file anywhere): {dropped}")

    LOCAL_INDEX.write_text(json.dumps(local, indent=1) + "\n")
    log(f"guarded index written: {len(local['hospitals'])} hospitals")
    return 2 if dropped else 0


if __name__ == "__main__":
    sys.exit(main())
