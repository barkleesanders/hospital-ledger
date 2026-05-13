#!/usr/bin/env python3
"""Watch the Mac mini Hospital Ledger runner and escalate issues to OpenClaw."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RUNNER_STATUS = DATA_DIR / "macmini_openclaw_pipeline.status.json"
CMS_STATUS = DATA_DIR / "cms_validation_monitor.status.json"
WATCHDOG_STATUS = DATA_DIR / "openclaw_pipeline_watchdog.status.json"
WATCHDOG_LOG = DATA_DIR / "openclaw_pipeline_watchdog.log"
WATCHDOG_STATE = DATA_DIR / "openclaw_pipeline_watchdog.state.json"
DEFAULT_TARGET = "8335979324"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def append_log(line: str) -> None:
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with WATCHDOG_LOG.open("a") as handle:
        handle.write(f"[{iso_now()}] {line}\n")


def read_json(path: Path) -> dict:
    try:
        with path.open() as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def parse_ts(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(status: dict) -> float | None:
    updated = parse_ts(status.get("updated_at"))
    if updated is None:
        return None
    return (now_utc() - updated.astimezone(dt.UTC)).total_seconds()


def tmux_has_session(name: str) -> bool:
    proc = subprocess.run(
        ["tmux", "has-session", "-t", name],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def openclaw_bin() -> str:
    candidates = ["/opt/homebrew/bin/openclaw", shutil.which("openclaw") or ""]
    return next((path for path in candidates if path and Path(path).exists()), "")


def run_openclaw_agent(message: str, *, target: str, dry_run: bool) -> None:
    openclaw = openclaw_bin()
    if not openclaw:
        append_log(f"openclaw missing; cannot escalate: {message}")
        return
    cmd = [
        openclaw,
        "agent",
        "--channel",
        "telegram",
        "--to",
        target,
        "--deliver",
        "--timeout",
        "900",
        "--message",
        message[:12000],
    ]
    append_log("$ " + " ".join(cmd[:9]) + " <message>")
    if dry_run:
        return
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        append_log(f"openclaw agent escalation failed: {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:2000]}")


def issue_key(issue: dict) -> str:
    raw = json.dumps(issue, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def should_escalate(key: str, cooldown_seconds: int) -> bool:
    state = read_json(WATCHDOG_STATE)
    last = state.get(key)
    if isinstance(last, str):
        last_ts = parse_ts(last)
        if last_ts and (now_utc() - last_ts.astimezone(dt.UTC)).total_seconds() < cooldown_seconds:
            return False
    state[key] = iso_now()
    write_json(WATCHDOG_STATE, state)
    return True


def runner_chunk_status(runner: dict) -> dict:
    chunk = runner.get("chunk")
    if not isinstance(chunk, int):
        return {}
    return read_json(DATA_DIR / "macmini_chunks" / f"chunk-{chunk:04d}.status.json")


def collect_issues(args: argparse.Namespace) -> list[dict]:
    issues: list[dict] = []
    runner = read_json(RUNNER_STATUS)
    cms = read_json(CMS_STATUS)
    runner_session = tmux_has_session("hospital-ledger-openclaw")
    cms_session = tmux_has_session("hospital-ledger-cms-validate")

    if not runner_session and runner.get("phase") not in ("done",):
        issues.append({"kind": "runner_tmux_missing", "status": runner})
    if not cms_session:
        issues.append({"kind": "cms_validator_tmux_missing", "status": cms})

    runner_phase = runner.get("phase")
    if runner_phase == "error":
        issues.append({"kind": "runner_error", "status": runner})

    runner_age = age_seconds(runner)
    chunk_status = runner_chunk_status(runner)
    chunk_age = age_seconds(chunk_status)
    if runner_phase not in ("done", "error") and runner_age is not None and runner_age > args.runner_stale_seconds:
        if not chunk_status or chunk_age is None or chunk_age > args.chunk_stale_seconds:
            issues.append(
                {
                    "kind": "runner_stale",
                    "runner_age_seconds": int(runner_age),
                    "chunk_age_seconds": None if chunk_age is None else int(chunk_age),
                    "status": runner,
                    "chunk_status": chunk_status,
                }
            )

    cms_errors = int(cms.get("errors", 0) or 0)
    if cms_errors > 0:
        issues.append({"kind": "cms_validation_errors", "status": cms})

    free_gb = round(shutil.disk_usage(ROOT).free / (1024 ** 3), 2)
    if free_gb < args.min_free_gb:
        issues.append({"kind": "low_disk", "free_gb": free_gb})

    return issues


def escalation_prompt(issue: dict) -> str:
    return "\n".join(
        [
            "/goal Hospital Ledger Mac mini watchdog detected an issue.",
            "",
            "You are running on the Mac mini at /Users/barkleesanders/projects/hospital-ledger.",
            "Inspect these first:",
            "- data/macmini_openclaw_pipeline.status.json",
            "- data/macmini_openclaw_pipeline.log",
            "- data/cms_validation_monitor.status.json",
            "- data/openclaw_pipeline_watchdog.status.json",
            "",
            "Keep the parser on the Mac mini. Preserve RESOURCE_PERCENT=70 and two-worker behavior unless the error requires lowering it.",
            "If a chunk upload failed, fix Cloudflare/Wrangler auth or resume with SKIP_INGEST=1 SKIP_SLIM=1 UPLOAD_R2=1 CCNS_FILE=<chunk file>.",
            "If parsing stalled, inspect the current chunk status/failure file and use /carmack to patch the failing script.",
            "Do not deploy Pages directly here; leave deploy for /ship after parse and R2 upload finish.",
            "",
            "Watchdog issue JSON:",
            json.dumps(issue, indent=2, sort_keys=True)[:8000],
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("WATCHDOG_POLL_SECONDS", "60")))
    parser.add_argument("--cooldown-seconds", type=int, default=int(os.environ.get("WATCHDOG_COOLDOWN_SECONDS", "1800")))
    parser.add_argument("--runner-stale-seconds", type=int, default=int(os.environ.get("WATCHDOG_RUNNER_STALE_SECONDS", "1800")))
    parser.add_argument("--chunk-stale-seconds", type=int, default=int(os.environ.get("WATCHDOG_CHUNK_STALE_SECONDS", "1800")))
    parser.add_argument("--min-free-gb", type=float, default=float(os.environ.get("WATCHDOG_MIN_FREE_GB", "10")))
    parser.add_argument("--notify-target", default=os.environ.get("OPENCLAW_NOTIFY_TARGET", DEFAULT_TARGET))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    append_log("starting Hospital Ledger OpenClaw watchdog")
    while True:
        issues = collect_issues(args)
        status = {
            "phase": "watching",
            "updated_at": iso_now(),
            "host": socket.gethostname(),
            "issues": issues,
            "issue_count": len(issues),
            "runner_session": tmux_has_session("hospital-ledger-openclaw"),
            "cms_validator_session": tmux_has_session("hospital-ledger-cms-validate"),
            "free_gb": round(shutil.disk_usage(ROOT).free / (1024 ** 3), 2),
        }
        write_json(WATCHDOG_STATUS, status)
        for issue in issues:
            key = issue_key(issue)
            if should_escalate(key, args.cooldown_seconds):
                append_log(f"escalating {issue.get('kind')}: {key}")
                run_openclaw_agent(escalation_prompt(issue), target=args.notify_target, dry_run=args.dry_run)
        if args.once:
            break
        time.sleep(max(10, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
