#!/usr/bin/env python3
"""Mac mini/OpenClaw runner for the Hospital Ledger heavy pipeline."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SCRIPTS = ROOT / "scripts"
DB_PATH = ROOT / "db" / "hospital_ledger.db"
STATUS_FILE = DATA_DIR / "macmini_openclaw_pipeline.status.json"
LOG_FILE = DATA_DIR / "macmini_openclaw_pipeline.log"
HANDOFF_FILE = DATA_DIR / "openclaw_ship_handoff.md"
CHUNK_DIR = DATA_DIR / "macmini_chunks"
DEFAULT_OPENCLAW_TARGET = "8335979324"


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def write_status(phase: str, **extra: object) -> None:
    payload = {
        "phase": phase,
        "updated_at": now_utc(),
        "host": socket.gethostname(),
        "root": str(ROOT),
        **extra,
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(STATUS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATUS_FILE)


def append_log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as handle:
        handle.write(f"[{now_utc()}] {line}\n")


def run(cmd: list[str], *, env: dict[str, str] | None = None, dry_run: bool = False) -> int:
    printable = " ".join(shlex.quote(part) for part in cmd)
    append_log(f"$ {printable}")
    print(f"$ {printable}", flush=True)
    if dry_run:
        return 0
    with LOG_FILE.open("a") as handle:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.wait()


def require_ok(returncode: int, label: str) -> None:
    if returncode != 0:
        raise RuntimeError(f"{label} failed with exit {returncode}; see {LOG_FILE}")


def python_bin() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def memory_bytes() -> int | None:
    try:
        proc = subprocess.run(["sysctl", "-n", "hw.memsize"], text=True, capture_output=True, check=True)
        return int(proc.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def resource_plan(args: argparse.Namespace) -> dict[str, int | float | None]:
    cpu_count = os.cpu_count() or 2
    mem_bytes = memory_bytes()
    mem_gb = mem_bytes / (1024 ** 3) if mem_bytes else None
    cpu_workers = max(1, math.floor(cpu_count * args.resource_percent / 100))
    mem_workers = cpu_workers
    if mem_gb:
        mem_workers = max(1, math.floor((mem_gb * args.resource_percent / 100) / args.parser_gb_per_worker))
    total_workers = min(cpu_workers, mem_workers, args.max_workers)
    shards = args.shards or min(4, max(1, total_workers))
    workers_per_shard = args.workers_per_shard or max(1, total_workers // shards)
    total_workers = shards * workers_per_shard
    return {
        "cpu_count": cpu_count,
        "memory_gb": round(mem_gb, 2) if mem_gb else None,
        "resource_percent": args.resource_percent,
        "parser_gb_per_worker": args.parser_gb_per_worker,
        "shards": shards,
        "workers_per_shard": workers_per_shard,
        "total_workers": total_workers,
    }


def preflight(args: argparse.Namespace, plan: dict[str, int | float | None]) -> None:
    missing = []
    for tool in ("tmux",):
        if not command_exists(tool):
            missing.append(tool)
    if not args.skip_upload and not command_exists("wrangler"):
        missing.append("wrangler")
    if missing:
        raise RuntimeError(f"missing required tools: {', '.join(missing)}")
    if not DB_PATH.exists():
        write_status("building_db", plan=plan)
        require_ok(run([python_bin(), str(SCRIPTS / "build_db.py")], dry_run=args.dry_run), "build_db")


def disk_free_gb() -> float:
    usage = shutil.disk_usage(ROOT)
    return round(usage.free / (1024 ** 3), 2)


def live_target_ccns() -> list[str]:
    import sqlite3

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


def price_ok(ccn: str) -> bool:
    path = ROOT / "site" / "data" / "prices" / f"{ccn}.json"
    if not path.exists():
        return False
    try:
        with path.open() as handle:
            payload = json.load(handle)
        return int(payload.get("n_slim", 0) or 0) > 0
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def chunked_parse(args: argparse.Namespace, plan: dict[str, int | float | None]) -> None:
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    ccns = live_target_ccns()
    if args.state:
        import sqlite3

        conn = sqlite3.connect(DB_PATH)
        try:
            allowed = {
                row[0]
                for row in conn.execute("SELECT ccn FROM hospitals WHERE state = ?", (args.state.upper(),)).fetchall()
            }
        finally:
            conn.close()
        ccns = [ccn for ccn in ccns if ccn in allowed]
    if args.offset > 0:
        ccns = ccns[args.offset:]
    if args.limit > 0:
        ccns = ccns[:args.limit]
    pending = [ccn for ccn in ccns if args.reparse_all or not price_ok(ccn)]
    chunk_size = max(1, args.chunk_size)
    total_chunks = math.ceil(len(pending) / chunk_size) if pending else 0
    write_status("chunking", plan=plan, total=len(ccns), pending=len(pending), chunk_size=chunk_size, free_gb=disk_free_gb())
    for index in range(total_chunks):
        chunk = pending[index * chunk_size:(index + 1) * chunk_size]
        chunk_file = CHUNK_DIR / f"chunk-{index + 1:04d}.ccns.txt"
        chunk_file.write_text("\n".join(chunk) + "\n")
        free_gb = disk_free_gb()
        if free_gb < args.min_free_gb:
            raise RuntimeError(f"free disk {free_gb}GiB is below minimum {args.min_free_gb}GiB")
        write_status(
            "parsing_chunk",
            plan=plan,
            chunk=index + 1,
            chunks=total_chunks,
            chunk_count=len(chunk),
            free_gb=free_gb,
        )
        require_ok(
            run(
                [
                    python_bin(),
                    str(SCRIPTS / "batch_ingest.py"),
                    "--ccns-file",
                    str(chunk_file),
                    "--workers",
                    str(plan["total_workers"]),
                    "--progress-every",
                    "25",
                    "--item-timeout-seconds",
                    str(args.item_timeout_seconds),
                    "--status-file",
                    str(CHUNK_DIR / f"chunk-{index + 1:04d}.status.json"),
                    "--failures-file",
                    str(CHUNK_DIR / f"chunk-{index + 1:04d}.failures.jsonl"),
                ],
                dry_run=args.dry_run,
            ),
            f"batch_ingest chunk {index + 1}",
        )
        slim_env = os.environ.copy()
        slim_env["CCNS_FILE"] = str(chunk_file)
        slim_env["SLIM_KEEP_STALE"] = "1"
        slim_env["SLIM_MERGE_INDEX"] = "1"
        slim_env["SLIM_INDEX_FROM_PRICES"] = "1"
        write_status("slimming_chunk", chunk=index + 1, chunks=total_chunks)
        require_ok(run([python_bin(), str(SCRIPTS / "slim_parsed.py")], env=slim_env, dry_run=args.dry_run), f"slim chunk {index + 1}")
        if not args.skip_upload:
            upload_env = os.environ.copy()
            upload_env["CCNS_FILE"] = str(chunk_file)
            upload_env["SKIP_INGEST"] = "1"
            upload_env["SKIP_SLIM"] = "1"
            upload_env["UPLOAD_R2"] = "1"
            write_status("uploading_chunk", chunk=index + 1, chunks=total_chunks)
            require_ok(run([python_bin(), str(SCRIPTS / "stage4_refresh.py")], env=upload_env, dry_run=args.dry_run), f"upload chunk {index + 1}")
        if args.prune_parsed_after_upload and not args.dry_run:
            for ccn in chunk:
                parsed_path = DATA_DIR / "parsed" / f"{ccn}.json"
                if parsed_path.exists():
                    parsed_path.unlink()
            append_log(f"pruned parsed artifacts for chunk {index + 1}")


def finalize_chunked(args: argparse.Namespace) -> None:
    write_status("rebuilding_site_metadata", free_gb=disk_free_gb())
    require_ok(run([python_bin(), str(SCRIPTS / "build_site_data.py")], dry_run=args.dry_run), "build_site_data")
    slim_env = os.environ.copy()
    slim_env["SLIM_KEEP_STALE"] = "1"
    slim_env["SLIM_INDEX_FROM_PRICES"] = "1"
    require_ok(run([python_bin(), str(SCRIPTS / "slim_parsed.py")], env=slim_env, dry_run=args.dry_run), "rebuild indexes from price files")
    if not args.skip_upload:
        upload_env = os.environ.copy()
        upload_env["SKIP_INGEST"] = "1"
        upload_env["SKIP_SLIM"] = "1"
        upload_env["UPLOAD_R2"] = "1"
        write_status("uploading_final_site_artifacts", free_gb=disk_free_gb())
        require_ok(run([python_bin(), str(SCRIPTS / "stage4_refresh.py")], env=upload_env, dry_run=args.dry_run), "final R2 upload")


def notify(message: str, *, target: str, dry_run: bool) -> None:
    candidates = ["/opt/homebrew/bin/openclaw", shutil.which("openclaw") or ""]
    openclaw = next((path for path in candidates if path and Path(path).exists()), "")
    if not openclaw:
        append_log(f"openclaw not available; notification skipped: {message}")
        return
    cmd = [
        openclaw,
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        target,
        "--message",
        message[:3900],
    ]
    rc = run(cmd, dry_run=dry_run)
    if rc != 0:
        append_log(f"openclaw notification failed with exit {rc}")


def write_handoff(args: argparse.Namespace) -> None:
    HANDOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    deploy_line = (
        "The runner was allowed to deploy directly."
        if args.allow_deploy
        else "Deploy was intentionally left for OpenClaw /ship."
    )
    HANDOFF_FILE.write_text(
        "\n".join(
            [
                "# OpenClaw Ship Handoff",
                "",
                f"Generated: {now_utc()}",
                f"Runner status: {STATUS_FILE}",
                f"Runner log: {LOG_FILE}",
                f"Finalizer status: {DATA_DIR / 'finalize_ship_status.json'}",
                "",
                deploy_line,
                "",
                "Suggested OpenClaw prompt:",
                "",
                "```",
                "/goal Finish Hospital Ledger after the Mac mini local parser/R2 upload run. "
                "Read data/macmini_openclaw_pipeline.status.json and data/finalize_ship_status.json. "
                "Read data/cms_validation_monitor.status.json for official CMS compliance-validation sidecar status; "
                "do not use CMS validation output for extraction. "
                "If the runner phase is error, use /carmack to fix the failing script or API call. "
                "If upload completed and deploy is still pending, use /ship for the hospital-ledger Pages site, "
                "then verify https://hospital-ledger.pages.dev/data/summary.json and /api/prices-index.",
                "```",
                "",
            ]
        )
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-percent", type=int, default=int(os.environ.get("RESOURCE_PERCENT", "70")))
    parser.add_argument("--parser-gb-per-worker", type=float, default=float(os.environ.get("PARSER_GB_PER_WORKER", "5")))
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("MAX_TOTAL_WORKERS", "8")))
    parser.add_argument("--shards", type=int, default=int(os.environ.get("SHARDS", "0")))
    parser.add_argument("--workers-per-shard", type=int, default=int(os.environ.get("WORKERS_PER_SHARD", "0")))
    parser.add_argument("--item-timeout-seconds", type=int, default=int(os.environ.get("ITEM_TIMEOUT_SECONDS", "3600")))
    parser.add_argument("--retry-passes", type=int, default=int(os.environ.get("RETRY_PASSES", "2")))
    parser.add_argument("--chunk-size", type=int, default=int(os.environ.get("CHUNK_SIZE", "50")))
    parser.add_argument("--min-free-gb", type=float, default=float(os.environ.get("MIN_FREE_GB", "10")))
    parser.add_argument("--state", default=os.environ.get("STATE", ""))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("LIMIT", "0")))
    parser.add_argument("--offset", type=int, default=int(os.environ.get("OFFSET", "0")))
    parser.add_argument("--skip-parse", action="store_true")
    parser.add_argument("--full-standardize", action="store_true")
    parser.add_argument("--reparse-all", action="store_true")
    parser.add_argument("--prune-parsed-after-upload", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--allow-deploy", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notify-target", default=os.environ.get("OPENCLAW_NOTIFY_TARGET", DEFAULT_OPENCLAW_TARGET))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = resource_plan(args)
    write_handoff(args)
    write_status("starting", plan=plan, allow_deploy=args.allow_deploy)
    try:
        preflight(args, plan)
        if not args.skip_parse and not args.full_standardize:
            chunked_parse(args, plan)
        elif not args.skip_parse:
            parse_cmd = [
                python_bin(),
                str(SCRIPTS / "full_standardize.py"),
                "--shards",
                str(plan["shards"]),
                "--workers",
                str(plan["workers_per_shard"]),
                "--progress-every",
                "25",
                "--item-timeout-seconds",
                str(args.item_timeout_seconds),
            ]
            if args.state:
                parse_cmd.extend(["--state", args.state.upper()])
            if args.limit > 0:
                parse_cmd.extend(["--limit", str(args.limit)])
            if args.offset > 0:
                parse_cmd.extend(["--offset", str(args.offset)])
            write_status("parsing", plan=plan)
            require_ok(run(parse_cmd, dry_run=args.dry_run), "full_standardize")

        if not args.full_standardize:
            finalize_chunked(args)
            write_status("done", plan=plan, handoff=str(HANDOFF_FILE), free_gb=disk_free_gb())
            notify(f"Hospital Ledger Mac mini runner done. Status: {STATUS_FILE}. Handoff: {HANDOFF_FILE}", target=args.notify_target, dry_run=args.dry_run)
            return 0

        finalizer_cmd = [
            python_bin(),
            str(SCRIPTS / "finalize_ship.py"),
            "--wait-mode",
            "remaining",
            "--retry-passes",
            str(args.retry_passes),
            "--retry-workers",
            str(max(1, int(plan["workers_per_shard"] or 1))),
            "--retry-item-timeout-seconds",
            str(args.item_timeout_seconds),
            "--status-file",
            str(DATA_DIR / "finalize_ship_status.json"),
        ]
        if args.skip_upload:
            finalizer_cmd.append("--skip-upload")
        if not args.allow_deploy:
            finalizer_cmd.extend(["--skip-deploy", "--skip-verify"])
        elif args.skip_verify:
            finalizer_cmd.append("--skip-verify")

        write_status("finalizing", plan=plan, allow_deploy=args.allow_deploy)
        require_ok(run(finalizer_cmd, dry_run=args.dry_run), "finalize_ship")
        write_status("done", plan=plan, handoff=str(HANDOFF_FILE))
        notify(f"Hospital Ledger Mac mini runner done. Status: {STATUS_FILE}. Handoff: {HANDOFF_FILE}", target=args.notify_target, dry_run=args.dry_run)
        return 0
    except Exception as exc:
        write_status("error", error=f"{type(exc).__name__}: {exc}", handoff=str(HANDOFF_FILE))
        notify(f"Hospital Ledger Mac mini runner error: {exc}. Check {STATUS_FILE} and {LOG_FILE}.", target=args.notify_target, dry_run=args.dry_run)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
