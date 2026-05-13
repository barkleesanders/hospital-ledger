#!/usr/bin/env python3
"""Wait for the live-preview gap backfill, then retry, rebuild, upload, deploy, and verify."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SCRIPTS = ROOT / "scripts"
SITE_DIR = ROOT / "site"
PAGES_BUNDLE_DIR = DATA_DIR / "_pages_bundle"
GAP_STATUS_FILES = [
    DATA_DIR / "gap_backfill_sharded.status.json",
    DATA_DIR / "gap_backfill.status.json",
]
GAP_FAILURES_FILE = DATA_DIR / "gap_backfill.failures.jsonl"
GAP_SHARD_DIR = DATA_DIR / "gap_shards"
FINAL_STATUS_FILE = DATA_DIR / "finalize_gap_backfill.status.json"
PAGES_BRANCH = os.environ.get("PAGES_BRANCH", "main")
ROW_COUNT_RE = re.compile(rb'"row_count":(\d+)')
USER_AGENT = "Mozilla/5.0"
CF_GLOBAL_KEY_FILE = Path.home() / ".cloudflared" / "cf-global-api-key.json"


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def read_json(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def read_gap_status() -> dict | None:
    for path in GAP_STATUS_FILES:
        payload = read_json(path)
        if payload:
            return payload
    return None


def write_status(phase: str, **extra: object) -> None:
    payload = {
        "phase": phase,
        "updated_at": now_utc(),
        **extra,
    }
    tmp = FINAL_STATUS_FILE.with_suffix(FINAL_STATUS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(FINAL_STATUS_FILE)


def run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    printable = " ".join(cmd)
    print(f"$ {printable}", flush=True)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        check=check,
        capture_output=False,
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


def parsed_row_count(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return 0
    match = ROW_COUNT_RE.search(head)
    return int(match.group(1)) if match else 0


def count_good_parsed_files() -> int:
    parsed_dir = DATA_DIR / "parsed"
    return sum(1 for path in parsed_dir.glob("*.json") if parsed_row_count(path) > 0)


def count_local_price_index() -> dict[str, int]:
    path = SITE_DIR / "data" / "prices" / "index.json"
    try:
        with path.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"entries": 0, "usable": 0, "zero": 0}
    hospitals = data.get("hospitals", data) if isinstance(data, dict) else data
    usable = sum(
        1
        for item in hospitals
        if int(item.get("n") or item.get("count") or item.get("items") or 0) > 0
    )
    zero = len(hospitals) - usable
    return {"entries": len(hospitals), "usable": usable, "zero": zero}


def wait_for_gap_backfill(poll_seconds: int) -> dict:
    while True:
        status = read_gap_status()
        if status:
            done = int(status.get("done", 0) or 0)
            eligible = int(status.get("eligible", 0) or 0)
            pending = int(status.get("pending", 0) or 0)
            write_status(
                "waiting_for_gap_backfill",
                done=done,
                eligible=eligible,
                pending=pending,
                success=status.get("success"),
                failed=status.get("failed"),
                gap_updated_at=status.get("updated_at"),
            )
            print(
                f"waiting for gap backfill: {done}/{eligible} done, pending={pending}, "
                f"updated_at={status.get('updated_at')}",
                flush=True,
            )
            if eligible > 0 and pending == 0 and done >= eligible:
                return status
        else:
            write_status("waiting_for_gap_backfill", message="gap status file not available yet")
            print("waiting for gap status file", flush=True)
        time.sleep(poll_seconds)


def active_stage4_refresh_processes() -> list[str]:
    proc = subprocess.run(
        ["pgrep", "-af", "stage4_refresh.py"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return [line for line in lines if "finalize_gap_backfill.py" not in line]


def wait_for_stage4_idle(poll_seconds: int) -> None:
    while True:
        active = active_stage4_refresh_processes()
        write_status("waiting_for_stage4_idle", active_stage4_refresh=active)
        if not active:
            print("no stage4_refresh.py process is currently active", flush=True)
            return
        print("waiting for prior stage4_refresh.py process to finish", flush=True)
        for line in active:
            print(f"  active: {line}", flush=True)
        time.sleep(poll_seconds)


def gap_failure_files() -> list[Path]:
    files = []
    if GAP_FAILURES_FILE.exists() and GAP_FAILURES_FILE.stat().st_size > 0:
        files.append(GAP_FAILURES_FILE)
    for path in sorted(GAP_SHARD_DIR.glob("remaining-shard-*.failures.jsonl")):
        if path.exists() and path.stat().st_size > 0:
            files.append(path)
    return files


def run_retry_passes(pass_count: int, workers: int, item_timeout_seconds: int) -> list[dict[str, int]]:
    failures_files = gap_failure_files()
    if not failures_files:
        print("no gap failures file found; skipping retries", flush=True)
        return []

    results: list[dict[str, int]] = []
    for attempt in range(1, pass_count + 1):
        before = count_good_parsed_files()
        write_status(
            "retrying_failures",
            retry_pass=attempt,
            retry_workers=workers,
            retry_item_timeout_seconds=item_timeout_seconds,
            good_parsed_before=before,
            failure_files=[str(path) for path in failures_files],
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
                *[str(path) for path in failures_files],
            ],
            check=False,
        )
        after = count_good_parsed_files()
        result = {
            "pass": attempt,
            "returncode": proc.returncode,
            "good_parsed_before": before,
            "good_parsed_after": after,
        }
        results.append(result)
        print(f"retry pass {attempt}: {result}", flush=True)
        if after <= before:
            break
    return results


def rebuild_slim_prices() -> dict[str, int]:
    write_status("rebuilding_slim_prices")
    run([sys.executable, str(SCRIPTS / "slim_parsed.py")])
    counts = count_local_price_index()
    write_status("rebuilt_slim_prices", **counts)
    return counts


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
        [
            "wrangler",
            "pages",
            "deploy",
            ".",
            "--project-name",
            project_name,
            "--branch",
            PAGES_BRANCH,
            "--commit-dirty=true",
        ],
        cwd=PAGES_BUNDLE_DIR,
    )


def fetch_json(url: str, out_path: Path) -> object:
    if out_path.exists():
        out_path.unlink()
    run(["curl", "-A", USER_AGENT, "-fsS", url, "-o", str(out_path)])
    with out_path.open() as handle:
        return json.load(handle)


def verify_remote(base_url: str, sample_ccns: list[str]) -> dict[str, object]:
    ts = int(time.time())
    prices_index = fetch_json(
        f"{base_url}/api/prices-index?ts={ts}",
        DATA_DIR / "_verify_gap_prices_index.json",
    )
    hospitals = prices_index.get("hospitals", prices_index) if isinstance(prices_index, dict) else prices_index
    usable = sum(
        1
        for item in hospitals
        if int(item.get("n") or item.get("count") or item.get("items") or 0) > 0
    )
    zero = len(hospitals) - usable
    samples = []
    for ccn in sample_ccns:
        data = fetch_json(
            f"{base_url}/api/prices/{ccn}?ts={ts}",
            DATA_DIR / f"_verify_gap_{ccn}.json",
        )
        samples.append(
            {
                "ccn": str(data.get("ccn") or ccn),
                "n_slim": int(data.get("n_slim") or 0),
                "items": len(data.get("items") or []),
            }
        )
    summary = {
        "entries": len(hospitals),
        "usable": usable,
        "zero": zero,
        "samples": samples,
    }
    write_status("verified_remote", verify=summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--retry-passes", type=int, default=2)
    parser.add_argument("--retry-workers", type=int, default=3)
    parser.add_argument("--retry-item-timeout-seconds", type=int, default=600)
    parser.add_argument("--project-name", default="hospital-ledger")
    parser.add_argument("--verify-url", default="https://hospital-ledger.pages.dev")
    parser.add_argument("--sample-ccns", nargs="*", default=["050334", "110121", "310064"])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        write_status("starting", root=str(ROOT))
        wait_for_gap_backfill(args.poll_seconds)
        wait_for_stage4_idle(args.poll_seconds)
        retry_results = run_retry_passes(
            args.retry_passes,
            args.retry_workers,
            args.retry_item_timeout_seconds,
        )
        counts = rebuild_slim_prices()
        upload_r2()
        deploy_pages(args.project_name)
        verify = verify_remote(args.verify_url, args.sample_ccns)
        write_status("done", retry_results=retry_results, local=counts, verify=verify)
        return 0
    except Exception as exc:  # pragma: no cover - operator script
        write_status("error", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
