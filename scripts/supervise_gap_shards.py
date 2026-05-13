#!/usr/bin/env python3
"""Watch sharded gap backfill sessions and restart only stale or missing shards."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shlex
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_SHARD_DIR = DATA_DIR / "gap_shards"
DEFAULT_STATUS_FILE = DATA_DIR / "gap_shard_supervisor.status.json"
SHARD_STEM_RE = re.compile(r"remaining-shard-(\d+)$")


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def read_json(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def shard_specs(shard_dir: Path, session_prefix: str) -> list[dict]:
    specs: list[dict] = []
    for shard_file in sorted(shard_dir.glob("remaining-shard-*.ccns.txt")):
        stem = shard_file.name.replace(".ccns.txt", "")
        match = SHARD_STEM_RE.fullmatch(stem)
        if not match:
            continue
        shard_num = int(match.group(1))
        count = sum(1 for line in shard_file.read_text().splitlines() if line.strip())
        specs.append(
            {
                "stem": stem,
                "count": count,
                "session": f"{session_prefix}-{shard_num:02d}",
                "ccns_file": shard_file,
                "status_file": shard_dir / f"{stem}.status.json",
                "failures_file": shard_dir / f"{stem}.failures.jsonl",
                "log_file": shard_dir / f"{stem}.log",
            }
        )
    return specs


def build_tmux_command(
    session_name: str,
    shard_file: Path,
    *,
    workers_per_shard: int,
    item_timeout_seconds: int,
    progress_every: int,
    status_file: Path,
    failures_file: Path,
    log_file: Path,
) -> list[str]:
    ingest_cmd = [
        ".venv/bin/python",
        "-u",
        "scripts/batch_ingest.py",
        "--ccns-file",
        str(shard_file),
        "--resume",
        "--workers",
        str(workers_per_shard),
        "--item-timeout-seconds",
        str(item_timeout_seconds),
        "--progress-every",
        str(progress_every),
        "--status-file",
        str(status_file),
        "--failures-file",
        str(failures_file),
    ]
    shell_cmd = (
        f"cd {shlex.quote(str(ROOT))} && "
        f"{shell_join(ingest_cmd)} >> {shlex.quote(str(log_file))} 2>&1"
    )
    return ["tmux", "new-session", "-d", "-s", session_name, shell_cmd]


def tmux_session_exists(name: str) -> bool:
    proc = subprocess.run(
        ["tmux", "has-session", "-t", name],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def tmux_kill_session(name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", name],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def start_session(spec: dict, args: argparse.Namespace) -> None:
    spec["log_file"].parent.mkdir(parents=True, exist_ok=True)
    tmux_cmd = build_tmux_command(
        spec["session"],
        spec["ccns_file"],
        workers_per_shard=args.workers_per_shard,
        item_timeout_seconds=args.item_timeout_seconds,
        progress_every=args.progress_every,
        status_file=spec["status_file"],
        failures_file=spec["failures_file"],
        log_file=spec["log_file"],
    )
    subprocess.run(tmux_cmd, cwd=str(ROOT), check=True)


def freshest_file_info(spec: dict) -> tuple[Path | None, float | None]:
    freshest_path: Path | None = None
    freshest_mtime: float | None = None
    for path in (spec["status_file"], spec["log_file"]):
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if freshest_mtime is None or mtime > freshest_mtime:
            freshest_path = path
            freshest_mtime = mtime
    return freshest_path, freshest_mtime


def read_shard_snapshot(spec: dict) -> dict:
    status = read_json(spec["status_file"]) or {}
    skipped_existing = int(status.get("skipped_existing", 0) or 0)
    done = int(status.get("done", 0) or 0) + skipped_existing
    pending = int(status.get("pending", max(spec["count"] - done, 0)) or 0)
    freshest_path, freshest_mtime = freshest_file_info(spec)
    age_seconds = (
        max(0, int(time.time() - freshest_mtime))
        if freshest_mtime is not None
        else None
    )
    return {
        "done": done,
        "pending": pending,
        "success": int(status.get("success", 0) or 0),
        "failed": int(status.get("failed", 0) or 0),
        "skipped_existing": skipped_existing,
        "updated_at": status.get("updated_at"),
        "state": status.get("state") or "",
        "workers": int(status.get("workers", 0) or 0),
        "freshest_file": freshest_path.name if freshest_path else None,
        "freshest_mtime": freshest_mtime,
        "age_seconds": age_seconds,
    }


def append_restart_marker(log_file: Path, action: str, reason: str, age_seconds: int | None) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a") as handle:
        handle.write(
            f"\n[{now_utc()}] supervisor {action} reason={reason} age_seconds={age_seconds}\n"
        )


def restart_shard(
    spec: dict,
    args: argparse.Namespace,
    *,
    reason: str,
    age_seconds: int | None,
    session_exists: bool,
) -> dict:
    action = "restart" if session_exists else "start"
    append_restart_marker(spec["log_file"], action, reason, age_seconds)
    if session_exists:
        tmux_kill_session(spec["session"])
    start_session(spec, args)
    return {
        "at": now_utc(),
        "action": action,
        "reason": reason,
        "session": spec["session"],
        "shard": spec["stem"],
        "age_seconds": age_seconds,
    }


def summarize_state(rows: list[dict], actions_this_loop: list[dict]) -> str:
    if actions_this_loop:
        return "intervening"
    if rows and all(row["pending"] == 0 for row in rows):
        return "complete"
    if any(row["state"] == "stale" for row in rows):
        return "stale_detected"
    return "watching"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    parser.add_argument("--status-file", default=str(DEFAULT_STATUS_FILE))
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--stale-seconds", type=int, default=900)
    parser.add_argument("--restart-cooldown-seconds", type=int, default=900)
    parser.add_argument("--session-prefix", default="hospital-ledger-gap-shard")
    parser.add_argument("--workers-per-shard", type=int, default=2)
    parser.add_argument("--item-timeout-seconds", type=int, default=600)
    parser.add_argument("--progress-every", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shard_dir = Path(args.shard_dir)
    status_file = Path(args.status_file)
    specs = shard_specs(shard_dir, args.session_prefix)
    if not specs:
        print(f"no shard files found in {shard_dir}", flush=True)
        return 1

    started_at = now_utc()
    last_restart_epochs: dict[str, float] = {}
    recent_actions: list[dict] = []
    print(
        f"supervising {len(specs)} shard sessions from {shard_dir} with stale>{args.stale_seconds}s",
        flush=True,
    )

    while True:
        now_epoch = time.time()
        actions_this_loop: list[dict] = []
        rows: list[dict] = []

        for spec in specs:
            session_exists = tmux_session_exists(spec["session"])
            snapshot = read_shard_snapshot(spec)
            pending = int(snapshot["pending"])
            age_seconds = snapshot["age_seconds"]
            last_restart_epoch = last_restart_epochs.get(spec["session"])
            cooldown_left = None
            if last_restart_epoch is not None:
                cooldown_left = max(
                    0,
                    int(
                        math.ceil(
                            args.restart_cooldown_seconds - (now_epoch - last_restart_epoch)
                        )
                    ),
                )

            action = None
            state = "complete" if pending == 0 else "healthy"
            reason = ""

            if pending > 0 and not session_exists:
                state = "missing_session"
                reason = "missing_session"
            elif pending > 0 and age_seconds is None:
                state = "starting"
            elif pending > 0 and age_seconds is not None and age_seconds > args.stale_seconds:
                state = "stale"
                reason = f"no_updates_for_{age_seconds}s"

            should_restart = pending > 0 and reason and (cooldown_left in (None, 0))
            if should_restart:
                action = restart_shard(
                    spec,
                    args,
                    reason=reason,
                    age_seconds=age_seconds,
                    session_exists=session_exists,
                )
                last_restart_epochs[spec["session"]] = now_epoch
                recent_actions.append(action)
                actions_this_loop.append(action)
                state = "restarting"
                cooldown_left = args.restart_cooldown_seconds

            rows.append(
                {
                    "shard": spec["stem"],
                    "session": spec["session"],
                    "limit": spec["count"],
                    "done": snapshot["done"],
                    "pending": pending,
                    "success": snapshot["success"],
                    "failed": snapshot["failed"],
                    "skipped_existing": snapshot["skipped_existing"],
                    "workers": snapshot["workers"] or args.workers_per_shard,
                    "session_exists": session_exists,
                    "state": state,
                    "updated_at": snapshot["updated_at"],
                    "freshest_file": snapshot["freshest_file"],
                    "freshest_at": (
                        dt.datetime.fromtimestamp(snapshot["freshest_mtime"], tz=dt.UTC)
                        .isoformat(timespec="seconds")
                        if snapshot["freshest_mtime"] is not None
                        else None
                    ),
                    "age_seconds": age_seconds,
                    "cooldown_left_seconds": cooldown_left,
                    "last_action": action,
                }
            )

        recent_actions = recent_actions[-12:]
        stale_count = sum(1 for row in rows if row["state"] == "stale")
        missing_count = sum(1 for row in rows if row["state"] == "missing_session")
        payload = {
            "started_at": started_at,
            "updated_at": now_utc(),
            "state": summarize_state(rows, actions_this_loop),
            "poll_seconds": args.poll_seconds,
            "stale_seconds": args.stale_seconds,
            "restart_cooldown_seconds": args.restart_cooldown_seconds,
            "workers_per_shard": args.workers_per_shard,
            "item_timeout_seconds": args.item_timeout_seconds,
            "shard_count": len(rows),
            "stale_count": stale_count,
            "missing_count": missing_count,
            "recent_actions": recent_actions,
            "shards": rows,
        }
        write_json(status_file, payload)

        if actions_this_loop:
            for action in actions_this_loop:
                print(
                    f"{action['action']} {action['session']} reason={action['reason']} "
                    f"age={action['age_seconds']}",
                    flush=True,
                )
        else:
            healthy = sum(1 for row in rows if row["state"] in {"healthy", "starting", "complete"})
            print(
                f"watch {healthy}/{len(rows)} healthy_or_complete | "
                f"stale={stale_count} missing={missing_count}",
                flush=True,
            )

        if rows and all(row["pending"] == 0 for row in rows):
            return 0
        time.sleep(max(args.poll_seconds, 5.0))


if __name__ == "__main__":
    raise SystemExit(main())
