#!/usr/bin/env python3
"""Local-first Stage 4 refresh lane.

Runs a bounded ingest into data/parsed, rebuilds compact static site JSON, and
optionally mirrors canonical parsed/raw artifacts to Cloudflare R2. It never
deploys the Pages site.
"""
from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
import sys
import datetime as dt
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PARSED_DIR = ROOT / "data" / "parsed"
RAW_DIR = ROOT / "data" / "raw"
SITE_PRICE_DIR = ROOT / "site" / "data" / "prices"
CPT_INDEX = ROOT / "site" / "data" / "cpt-index.json"
WRANGLER_PUT_LIMIT_BYTES = 300 * 1024 * 1024
DEFAULT_R2_MANIFEST = ROOT / "data" / "r2_upload_manifest.json"
CF_GLOBAL_KEY_FILE = Path.home() / ".cloudflared" / "cf-global-api-key.json"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from None
    if parsed < 1:
        raise SystemExit(f"{name} must be >= 1")
    return parsed


def run(cmd: list[str], *, dry_run: bool = False) -> None:
    printable = " ".join(shlex.quote(part) for part in cmd)
    print(f"$ {printable}")
    if dry_run:
        return
    subprocess.run(cmd, cwd=ROOT, check=True)


def load_cloudflare_env() -> None:
    if os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CLOUDFLARE_API_KEY"):
        return
    if not CF_GLOBAL_KEY_FILE.exists():
        return
    try:
        with CF_GLOBAL_KEY_FILE.open() as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    email = str(payload.get("email") or "").strip()
    global_api_key = str(payload.get("global_api_key") or "").strip()
    account_id = str(payload.get("account_id") or "").strip()
    if email and global_api_key:
        os.environ.setdefault("CLOUDFLARE_EMAIL", email)
        os.environ.setdefault("CLOUDFLARE_API_KEY", global_api_key)
    if account_id:
        os.environ.setdefault("CLOUDFLARE_ACCOUNT_ID", account_id)


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open() as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def object_signature(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path.relative_to(ROOT)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def manifest_entry_matches(entry: object, signature: dict[str, object]) -> bool:
    if not isinstance(entry, dict):
        return False
    return all(entry.get(key) == value for key, value in signature.items())


def put_r2_object(
    bucket: str,
    key: str,
    path: Path,
    *,
    dry_run: bool = False,
    manifest: dict | None = None,
    force: bool = False,
    stats: dict[str, int] | None = None,
    upload_mode: str = "auto",
    rclone_remote: str = "r2-turbo",
) -> bool:
    stats = stats if stats is not None else {}
    if not path.exists():
        stats["missing"] = stats.get("missing", 0) + 1
        return False
    manifest_key = f"{bucket}/{key}"
    signature = object_signature(path)
    if manifest is not None and not force and manifest_entry_matches(manifest.get(manifest_key), signature):
        stats["unchanged"] = stats.get("unchanged", 0) + 1
        print(f"skipping unchanged r2://{manifest_key}")
        return True
    size = path.stat().st_size
    use_rclone = upload_mode == "rclone" or (
        upload_mode == "auto"
        and size > WRANGLER_PUT_LIMIT_BYTES
        and shutil.which("rclone")
        and rclone_remote
    )
    if use_rclone:
        remote_path = f"{rclone_remote.rstrip(':')}:{bucket}/{key}"
        run(
            [
                "rclone",
                "copyto",
                str(path),
                remote_path,
                "--size-only",
                "--s3-upload-cutoff",
                "200Mi",
                "--s3-upload-concurrency",
                os.environ.get("R2_RCLONE_UPLOAD_CONCURRENCY", "2"),
            ],
            dry_run=dry_run,
        )
        stats["uploaded"] = stats.get("uploaded", 0) + 1
        stats["rclone"] = stats.get("rclone", 0) + 1
        if manifest is not None and not dry_run:
            manifest[manifest_key] = signature | {"uploaded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
        return True
    if size > WRANGLER_PUT_LIMIT_BYTES:
        stats["too_large"] = stats.get("too_large", 0) + 1
        print(
            f"skipping r2://{bucket}/{key}: file is {size} bytes and exceeds "
            f"wrangler put limit {WRANGLER_PUT_LIMIT_BYTES}; set R2_UPLOAD_MODE=rclone"
        )
        return False
    run(["wrangler", "r2", "object", "put", f"{bucket}/{key}", "-f", str(path), "--remote"], dry_run=dry_run)
    stats["uploaded"] = stats.get("uploaded", 0) + 1
    if manifest is not None and not dry_run:
        manifest[manifest_key] = signature | {"uploaded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    return True


def ingest_command() -> list[str]:
    state = os.environ.get("STATE", "").strip().upper()
    ccns = [c for c in os.environ.get("CCNS", "").replace(",", " ").split() if c]
    ccns_file = os.environ.get("CCNS_FILE", "").strip()
    limit = env_int("LIMIT", 10)
    workers = env_int("WORKERS", 2)
    item_timeout_seconds = env_int("ITEM_TIMEOUT_SECONDS", 0) if os.environ.get("ITEM_TIMEOUT_SECONDS") else 0

    cmd = [sys.executable, str(SCRIPTS / "batch_ingest.py")]
    if ccns:
        cmd.extend(["--ccns", *ccns])
    elif ccns_file:
        cmd.extend(["--ccns-file", ccns_file])
    elif state:
        cmd.extend(["--state", state, "--limit", str(limit)])
    else:
        cmd.extend(["--limit", str(limit)])
    cmd.extend(["--workers", str(workers)])
    if item_timeout_seconds > 0:
        cmd.extend(["--item-timeout-seconds", str(item_timeout_seconds)])
    return cmd


def iter_upload_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)


def selected_ccns() -> set[str]:
    ccns = {
        ccn
        for ccn in os.environ.get("CCNS", "").replace(",", " ").split()
        if ccn.strip()
    }
    ccns_file = os.environ.get("CCNS_FILE", "").strip()
    if ccns_file:
        path = Path(ccns_file)
        if path.exists():
            for raw_line in path.read_text().splitlines():
                ccn = raw_line.strip()
                if ccn:
                    ccns.add(ccn)
    return ccns


def filter_ccn_files(paths: list[Path], ccns: set[str]) -> list[Path]:
    if not ccns:
        return paths
    return [path for path in paths if path.stem in ccns or path.name == "index.json"]


def upload_to_r2(*, dry_run: bool) -> None:
    load_cloudflare_env()
    parsed_bucket = os.environ.get("PARSED_R2_BUCKET", "hl-mrf-parsed")
    raw_bucket = os.environ.get("RAW_R2_BUCKET", "hl-mrf-raw")
    manifest_enabled = env_bool("R2_UPLOAD_MANIFEST", True)
    manifest_path = Path(os.environ.get("R2_UPLOAD_MANIFEST_FILE", str(DEFAULT_R2_MANIFEST)))
    force = env_bool("R2_FORCE", False)
    upload_mode = os.environ.get("R2_UPLOAD_MODE", "auto").strip().lower()
    if upload_mode not in {"auto", "wrangler", "rclone"}:
        raise SystemExit("R2_UPLOAD_MODE must be auto, wrangler, or rclone")
    rclone_remote = os.environ.get("R2_RCLONE_REMOTE", "r2-turbo")
    manifest = load_manifest(manifest_path) if manifest_enabled else None
    stats: dict[str, int] = {}
    target_ccns = selected_ccns()
    parsed_files = filter_ccn_files(iter_upload_files(PARSED_DIR, (".json",)), target_ccns)
    compact_price_files = filter_ccn_files(iter_upload_files(SITE_PRICE_DIR, (".json",)), target_ccns)
    raw_files = filter_ccn_files(iter_upload_files(RAW_DIR, (".bin", ".csv", ".json", ".ndjson", ".xlsx", ".xls", ".zip")), target_ccns)

    if target_ccns:
        print(f"CCNS filter active: uploading artifacts for {len(target_ccns)} selected hospitals")

    print(f"uploading {len(compact_price_files)} compact display JSON files to r2://{parsed_bucket}/prices/")
    for path in compact_price_files:
        key = f"prices/{path.relative_to(SITE_PRICE_DIR).as_posix()}"
        put_r2_object(
            parsed_bucket,
            key,
            path,
            dry_run=dry_run,
            manifest=manifest,
            force=force,
            stats=stats,
            upload_mode=upload_mode,
            rclone_remote=rclone_remote,
        )
    if CPT_INDEX.exists():
        put_r2_object(
            parsed_bucket,
            "indexes/cpt-index.json",
            CPT_INDEX,
            dry_run=dry_run,
            manifest=manifest,
            force=force,
            stats=stats,
            upload_mode=upload_mode,
            rclone_remote=rclone_remote,
        )

    print(f"uploading {len(parsed_files)} canonical parsed JSON files to r2://{parsed_bucket}/parsed/")
    for path in parsed_files:
        key = f"parsed/{path.relative_to(PARSED_DIR).as_posix()}"
        put_r2_object(
            parsed_bucket,
            key,
            path,
            dry_run=dry_run,
            manifest=manifest,
            force=force,
            stats=stats,
            upload_mode=upload_mode,
            rclone_remote=rclone_remote,
        )

    if raw_files:
        print(f"uploading {len(raw_files)} raw MRF snapshots to r2://{raw_bucket}/raw/")
    else:
        print(f"no raw snapshots found under {RAW_DIR}; skipping raw R2 upload")
    for path in raw_files:
        key = f"raw/{path.relative_to(RAW_DIR).as_posix()}"
        put_r2_object(
            raw_bucket,
            key,
            path,
            dry_run=dry_run,
            manifest=manifest,
            force=force,
            stats=stats,
            upload_mode=upload_mode,
            rclone_remote=rclone_remote,
        )
    if manifest is not None and not dry_run:
        save_manifest(manifest_path, manifest)
    print(
        "R2 upload summary: "
        f"uploaded={stats.get('uploaded', 0)} "
        f"unchanged={stats.get('unchanged', 0)} "
        f"rclone={stats.get('rclone', 0)} "
        f"too_large={stats.get('too_large', 0)} "
        f"missing={stats.get('missing', 0)}"
    )


def main() -> int:
    dry_run = env_bool("DRY_RUN", False)
    upload_r2 = env_bool("UPLOAD_R2", False)
    skip_ingest = env_bool("SKIP_INGEST", False)
    skip_slim = env_bool("SKIP_SLIM", False)

    print("stage4 refresh: local ingest -> static compact JSON")
    if dry_run:
        print("DRY_RUN=1: printing commands only")
    if skip_ingest:
        print("SKIP_INGEST=1: using existing data/parsed artifacts")
    else:
        run(ingest_command(), dry_run=dry_run)
    if skip_slim:
        print("SKIP_SLIM=1: using existing public/data/prices artifacts")
    else:
        run([sys.executable, str(SCRIPTS / "slim_parsed.py")], dry_run=dry_run)

    if upload_r2:
        upload_to_r2(dry_run=dry_run)
    else:
        print("UPLOAD_R2 is not set; skipping R2 upload")
    print("deploy skipped; run /ship or an explicit deploy command separately")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
