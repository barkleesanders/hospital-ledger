#!/usr/bin/env python3
"""Snapshot the remaining live-preview gap CCNs into disjoint shard files.

This is intended for a faster restart path:
1. Stop the current single-process gap run.
2. Generate a fresh remaining-only snapshot.
3. Launch 2-3 disjoint shard sessions with --resume.

Running shards while the old single-process gap run is still active will
duplicate work and can race on the same parsed output files, so this script
defaults to planning only.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PARSED_DIR = DATA_DIR / "parsed"
SCRIPTS_DIR = ROOT / "scripts"
SHARDED_STATUS_FILE = DATA_DIR / "gap_backfill_sharded.status.json"
ROW_COUNT_RE = re.compile(rb'"row_count":(\d+)')


def parsed_ok(ccn: str) -> bool:
    path = PARSED_DIR / f"{ccn}.json"
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return False
    match = ROW_COUNT_RE.search(head)
    return bool(match and int(match.group(1)) > 0)


def load_ccns(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def shard_ranges(count: int, shards: int) -> list[tuple[int, int]]:
    chunk = int(math.ceil(count / max(1, shards)))
    ranges = []
    start = 0
    while start < count:
        limit = min(chunk, count - start)
        ranges.append((start, limit))
        start += limit
    return ranges


def clean_prior_shard_artifacts(shard_dir: Path) -> None:
    patterns = (
        "remaining-shard-*.ccns.txt",
        "remaining-shard-*.status.json",
        "remaining-shard-*.failures.jsonl",
        "remaining-shard-*.log",
    )
    for pattern in patterns:
        for path in shard_dir.glob(pattern):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def current_gap_run_active() -> bool:
    proc = subprocess.run(
        ["tmux", "ls"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    return "hospital-ledger-gap:" in proc.stdout


def build_tmux_command(
    session_name: str,
    shard_file: Path,
    shard_index: int,
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
    shell_cmd = f"cd {shlex.quote(str(ROOT))} && {shell_join(ingest_cmd)} > {shlex.quote(str(log_file))} 2>&1"
    return ["tmux", "new-session", "-d", "-s", session_name, shell_cmd]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-ccns-file",
        default=str(DATA_DIR / "live_preview_gap_ccns.txt"),
    )
    parser.add_argument(
        "--snapshot-file",
        default=str(DATA_DIR / "gap_remaining_ccns.snapshot.txt"),
    )
    parser.add_argument(
        "--shard-dir",
        default=str(DATA_DIR / "gap_shards"),
    )
    parser.add_argument("--shards", type=int, default=2)
    parser.add_argument("--workers-per-shard", type=int, default=2)
    parser.add_argument("--item-timeout-seconds", type=int, default=600)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--session-prefix", default="hospital-ledger-gap-shard")
    parser.add_argument("--aggregate-session", default="hospital-ledger-gap-aggregate")
    parser.add_argument("--aggregate-log-file", default=str(DATA_DIR / "gap_backfill.log"))
    parser.add_argument("--launch", action="store_true")
    parser.add_argument(
        "--allow-current-gap-run",
        action="store_true",
        help="Permit launch even if the existing hospital-ledger-gap tmux session is active",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.shards < 1:
        raise SystemExit("--shards must be >= 1")
    if args.workers_per_shard < 1:
        raise SystemExit("--workers-per-shard must be >= 1")

    source_file = Path(args.source_ccns_file)
    snapshot_file = Path(args.snapshot_file)
    shard_dir = Path(args.shard_dir)
    shard_dir.mkdir(parents=True, exist_ok=True)
    clean_prior_shard_artifacts(shard_dir)

    source_ccns = load_ccns(source_file)
    remaining_ccns = [ccn for ccn in source_ccns if not parsed_ok(ccn)]
    snapshot_file.write_text("\n".join(remaining_ccns) + ("\n" if remaining_ccns else ""))

    ranges = shard_ranges(len(remaining_ccns), args.shards)
    shard_rows = []
    for index, (offset, limit) in enumerate(ranges, start=1):
        shard_ccns = remaining_ccns[offset:offset + limit]
        shard_file = shard_dir / f"remaining-shard-{index:02d}.ccns.txt"
        shard_file.write_text("\n".join(shard_ccns) + ("\n" if shard_ccns else ""))
        status_file = shard_dir / f"remaining-shard-{index:02d}.status.json"
        failures_file = shard_dir / f"remaining-shard-{index:02d}.failures.jsonl"
        log_file = shard_dir / f"remaining-shard-{index:02d}.log"
        session_name = f"{args.session_prefix}-{index:02d}"
        tmux_cmd = build_tmux_command(
            session_name,
            shard_file,
            index,
            workers_per_shard=args.workers_per_shard,
            item_timeout_seconds=args.item_timeout_seconds,
            progress_every=args.progress_every,
            status_file=status_file,
            failures_file=failures_file,
            log_file=log_file,
        )
        shard_rows.append(
            {
                "index": index,
                "count": len(shard_ccns),
                "session": session_name,
                "shard_file": shard_file,
                "status_file": status_file,
                "failures_file": failures_file,
                "log_file": log_file,
                "tmux_cmd": tmux_cmd,
            }
        )

    print(f"source_gap_ccns={len(source_ccns)}")
    print(f"remaining_without_parsed_rows={len(remaining_ccns)}")
    print(f"snapshot_file={snapshot_file}")
    print(f"shards={len(shard_rows)} workers_per_shard={args.workers_per_shard} total_workers={len(shard_rows) * args.workers_per_shard}")
    print()
    for row in shard_rows:
        print(
            f"shard {row['index']:02d}: count={row['count']} session={row['session']} "
            f"file={row['shard_file']}"
        )

    print()
    print("launch commands:")
    for row in shard_rows:
        print(shell_join(row["tmux_cmd"]))
    aggregate_cmd = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        args.aggregate_session,
        (
            f"cd {shlex.quote(str(ROOT))} && "
            f"{shlex.quote(str(Path(os.sys.executable)))} -u "
            f"{shlex.quote(str(SCRIPTS_DIR / 'aggregate_gap_shards.py'))} "
            f"--status-file {shlex.quote(str(SHARDED_STATUS_FILE))} "
            f"> {shlex.quote(str(Path(args.aggregate_log_file)))} 2>&1"
        ),
    ]
    print(shell_join(aggregate_cmd))

    if not args.launch:
        print()
        print("planning only: rerun with --launch after stopping the old hospital-ledger-gap session")
        return 0

    if current_gap_run_active() and not args.allow_current_gap_run:
        print()
        print("refusing to launch because tmux session hospital-ledger-gap is still active")
        print("stop it first, then rerun with --launch")
        return 2

    for row in shard_rows:
        subprocess.run(row["tmux_cmd"], cwd=str(ROOT), check=True)
    subprocess.run(aggregate_cmd, cwd=str(ROOT), check=True)
    print()
    print("launched shard sessions:")
    for row in shard_rows:
        print(f"  {row['session']}")
    print(f"  {args.aggregate_session}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
