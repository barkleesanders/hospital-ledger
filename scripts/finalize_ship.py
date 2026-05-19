#!/usr/bin/env python3
"""Wait for the full run, retry failures, rebuild artifacts, upload, deploy, and verify."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SHARD_DIR = DATA_DIR / "full_standardize_shards"
AGG_STATUS_FILE = DATA_DIR / "full_standardize_parallel_status.json"
FINAL_STATUS_FILE = DATA_DIR / "finalize_ship_status.json"
STATUS_FILE = FINAL_STATUS_FILE
SCRIPTS = ROOT / "scripts"
SITE_DIR = ROOT / "site"
PAGES_BUNDLE_DIR = DATA_DIR / "_pages_bundle"
PAGES_BRANCH = os.environ.get("PAGES_BRANCH", "main")
DB_PATH = ROOT / "db" / "hospital_ledger.db"
ROW_COUNT_RE = re.compile(rb'"row_count":(\d+)')
CF_GLOBAL_KEY_FILE = Path.home() / ".cloudflared" / "cf-global-api-key.json"


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def read_json(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def write_status(phase: str, **extra: object) -> None:
    payload = {
        "phase": phase,
        "updated_at": now_utc(),
        **extra,
    }
    tmp = STATUS_FILE.with_suffix(STATUS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATUS_FILE)


def run(cmd: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    printable = " ".join(cmd)
    print(f"$ {printable}", flush=True)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        check=check,
        text=True,
    )


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


def total_from_aggregate(agg: dict) -> int:
    shards = agg.get("shards") or []
    total = sum(int(shard.get("limit", 0) or 0) for shard in shards)
    if total > 0:
        return total
    return int(agg.get("done", 0) or 0) + int(agg.get("pending", 0) or 0)


def live_target_ccns() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT h.ccn
            FROM hospitals h
            WHERE h.hospital_type IN ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
              AND (
                h.ccn IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
                OR h.ccn IN (
                    SELECT s.ccn
                    FROM mrf_seed s
                    JOIN mrf_probe p ON p.ccn = s.ccn AND p.mrf_url = s.mrf_url
                    WHERE p.alive=1
                )
              )
            ORDER BY h.name
            """
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def parsed_row_count(ccn: str) -> int:
    """Return parsed row count for a CCN. Reads .json or .json.gz transparently."""
    for path in (DATA_DIR / "parsed" / f"{ccn}.json", DATA_DIR / "parsed" / f"{ccn}.json.gz"):
        if not path.exists():
            continue
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rb") as handle:
                    head = handle.read(512)
            else:
                with path.open("rb") as handle:
                    head = handle.read(512)
        except (OSError, gzip.BadGzipFile):
            continue
        match = ROW_COUNT_RE.search(head)
        if match:
            return int(match.group(1))
    return 0


def corpus_progress() -> tuple[int, int, int]:
    ccns = live_target_ccns()
    done = 0
    for ccn in ccns:
        if parsed_row_count(ccn) > 0:
            done += 1
    total = len(ccns)
    pending = total - done
    return total, done, pending


def wait_for_full_run(poll_seconds: int, wait_mode: str) -> dict:
    if wait_mode == "remaining":
        while True:
            total, done, pending = corpus_progress()
            agg = {
                "done": done,
                "pending": pending,
                "updated_at": now_utc(),
            }
            write_status(
                "waiting_for_full",
                wait_mode=wait_mode,
                aggregate_done=done,
                aggregate_total=total,
                aggregate_pending=pending,
                aggregate_updated_at=agg.get("updated_at"),
            )
            print(
                f"waiting for remaining parse completion: {done}/{total} done, pending={pending}",
                flush=True,
            )
            if total > 0 and pending == 0:
                return agg | {"total": total}
            time.sleep(poll_seconds)

    while True:
        agg = read_json(AGG_STATUS_FILE)
        if not agg:
            write_status("waiting_for_full", message="aggregate status file not available yet")
            time.sleep(poll_seconds)
            continue
        total = total_from_aggregate(agg)
        done = int(agg.get("done", 0) or 0)
        pending = int(agg.get("pending", 0) or 0)
        write_status(
            "waiting_for_full",
            aggregate_done=done,
            aggregate_total=total,
            aggregate_pending=pending,
            aggregate_updated_at=agg.get("updated_at"),
            aggregate_rate_per_second=agg.get("rate_per_second"),
            aggregate_eta_seconds=agg.get("eta_seconds"),
        )
        print(
            f"waiting for full run: {done}/{total} done, pending={pending}, "
            f"updated_at={agg.get('updated_at')}",
            flush=True,
        )
        if total > 0 and done >= total and pending == 0:
            return agg
        time.sleep(poll_seconds)


def count_outputs() -> dict[str, int]:
    return {
        "parsed_json_files": len(list((DATA_DIR / "parsed").glob("*.json"))),
        "price_json_files": len(list((SITE_DIR / "data" / "prices").glob("*.json"))),
    }


def failure_files() -> list[str]:
    paths = sorted(glob.glob(str(SHARD_DIR / "*.failures.jsonl")))
    default_failures = DATA_DIR / "full_standardize_failures.jsonl"
    if default_failures.exists():
        paths.append(str(default_failures))
    return [path for path in paths if os.path.getsize(path) > 0]


def run_retry_passes(pass_count: int, workers: int, item_timeout_seconds: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    files = failure_files()
    if not files:
        print("no failure logs found; skipping retries", flush=True)
        return results

    for attempt in range(1, pass_count + 1):
        before = count_outputs()
        write_status(
            "retrying_failures",
            retry_pass=attempt,
            retry_workers=workers,
            retry_failure_files=files,
            **before,
        )
        proc = run(
            [
                sys.executable,
                str(SCRIPTS / "retry_failed_ingest.py"),
                "--workers",
                str(workers),
                "--item-timeout-seconds",
                str(item_timeout_seconds),
                "--failures-file",
                *files,
            ],
            check=False,
        )
        after = count_outputs()
        result = {
            "pass": attempt,
            "returncode": proc.returncode,
            "parsed_before": before["parsed_json_files"],
            "parsed_after": after["parsed_json_files"],
            "prices_before": before["price_json_files"],
            "prices_after": after["price_json_files"],
        }
        results.append(result)
        print(f"retry pass {attempt}: {result}", flush=True)
        if after["parsed_json_files"] <= before["parsed_json_files"]:
            break
    return results


def rebuild_site_data() -> dict[str, int]:
    write_status("rebuilding_site_artifacts")
    run([sys.executable, str(SCRIPTS / "build_site_data.py")])
    run([sys.executable, str(SCRIPTS / "slim_parsed.py")])
    outputs = count_outputs()
    write_status("rebuilt_site_artifacts", **outputs)
    return outputs


def upload_r2() -> None:
    env = os.environ.copy()
    env["SKIP_INGEST"] = "1"
    env["SKIP_SLIM"] = "1"
    env["UPLOAD_R2"] = "1"
    write_status("uploading_r2")
    run([sys.executable, str(SCRIPTS / "stage4_refresh.py")], env=env)


def deploy_pages(project_name: str) -> None:
    load_cloudflare_env()
    write_status("deploying_pages", project_name=project_name, branch=PAGES_BRANCH)
    run([sys.executable, str(SCRIPTS / "stage_pages_bundle.py"), "--output", str(PAGES_BUNDLE_DIR)])
    run(
        ["wrangler", "pages", "deploy", ".", "--project-name", project_name, "--branch", PAGES_BRANCH, "--commit-dirty=true"],
        cwd=PAGES_BUNDLE_DIR,
    )


def verify_remote(base_url: str) -> dict[str, object]:
    sample_price_ccn = next(
        (
            path.stem
            for path in sorted((SITE_DIR / "data" / "prices").glob("*.json"))
            if path.stem.isdigit() and len(path.stem) == 6
        ),
        None,
    )
    checks = [
        f"{base_url}/data/summary.json",
        f"{base_url}/data/hospitals.json",
        f"{base_url}/data/prices/index.json",
        f"{base_url}/api/prices-index",
        f"{base_url}/api/cpt-index",
    ]
    if sample_price_ccn:
        checks.append(f"{base_url}/api/prices/{sample_price_ccn}")
    results: list[dict[str, object]] = []
    for url in checks:
        proc = subprocess.run(
            ["curl", "-fsS", "-o", "/dev/null", "-w", "%{http_code}", url],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )
        results.append(
            {
                "url": url,
                "returncode": proc.returncode,
                "http_status": proc.stdout.strip() or None,
                "stderr": proc.stderr.strip() or None,
            }
        )
    write_status("verified_remote", verify_results=results)
    return {"verify_results": results}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--wait-mode", choices=["aggregate", "remaining"], default="aggregate")
    parser.add_argument("--retry-passes", type=int, default=2)
    parser.add_argument("--retry-workers", type=int, default=4)
    parser.add_argument("--retry-item-timeout-seconds", type=int, default=3600)
    parser.add_argument("--project-name", default="hospital-ledger")
    parser.add_argument("--status-file", default=str(FINAL_STATUS_FILE))
    parser.add_argument("--verify-url", default="https://hospital-ledger.pages.dev")
    parser.add_argument("--skip-wait", action="store_true")
    parser.add_argument("--skip-retry", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global STATUS_FILE
    STATUS_FILE = Path(args.status_file)
    try:
        write_status("starting", root=str(ROOT))
        if not args.skip_wait:
            agg = wait_for_full_run(args.poll_seconds, args.wait_mode)
            write_status(
                "full_run_complete",
                aggregate_done=agg.get("done"),
                aggregate_pending=agg.get("pending"),
                aggregate_total=agg.get("total", total_from_aggregate(agg)),
                aggregate_updated_at=agg.get("updated_at"),
            )
        if not args.skip_retry:
            retry_results = run_retry_passes(
                args.retry_passes,
                args.retry_workers,
                args.retry_item_timeout_seconds,
            )
            write_status("retry_complete", retry_results=retry_results)
        outputs = rebuild_site_data()
        if not args.skip_upload:
            upload_r2()
        if not args.skip_deploy:
            deploy_pages(args.project_name)
        verify_summary: dict[str, object] = {}
        if not args.skip_verify:
            verify_summary = verify_remote(args.verify_url)
        write_status("done", **outputs, **verify_summary)
        return 0
    except Exception as exc:  # pragma: no cover - operator script
        write_status("error", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
