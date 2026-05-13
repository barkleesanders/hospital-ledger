#!/usr/bin/env python3
"""Aggregate sharded remaining-gap ingest status into the canonical gap status file."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_SHARD_DIR = DATA_DIR / "gap_shards"
DEFAULT_STATUS_FILE = DATA_DIR / "gap_backfill_sharded.status.json"
DEFAULT_MERGED_FAILURES_FILE = DATA_DIR / "gap_backfill.failures.jsonl"


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


def shard_specs(shard_dir: Path) -> list[dict]:
    specs = []
    for shard_file in sorted(shard_dir.glob("remaining-shard-*.ccns.txt")):
        stem = shard_file.name.replace(".ccns.txt", "")
        count = sum(1 for line in shard_file.read_text().splitlines() if line.strip())
        specs.append(
            {
                "stem": stem,
                "ccns_file": shard_file,
                "status_file": shard_dir / f"{stem}.status.json",
                "failures_file": shard_dir / f"{stem}.failures.jsonl",
                "log_file": shard_dir / f"{stem}.log",
                "limit": count,
            }
        )
    return specs


def aggregate(specs: list[dict], started_at: str) -> dict:
    done = success = failed = pending = total_items = skipped_existing = workers = 0
    rate = 0.0
    recent_failures: list[dict] = []
    shards = []
    for spec in specs:
        status = read_json(spec["status_file"])
        if not status:
            shards.append(
                {
                    "shard": spec["stem"],
                    "limit": spec["limit"],
                    "done": 0,
                    "pending": spec["limit"],
                    "success": 0,
                    "failed": 0,
                    "skipped_existing": 0,
                    "rate_per_second": 0,
                    "state": "starting",
                }
            )
            pending += spec["limit"]
            continue

        shard_skipped = int(status.get("skipped_existing", 0) or 0)
        shard_done = int(status.get("done", 0) or 0)
        shard_pending = int(status.get("pending", 0) or 0)
        shards.append(
            {
                "shard": spec["stem"],
                "limit": spec["limit"],
                "done": shard_done,
                "pending": shard_pending,
                "success": int(status.get("success", 0) or 0),
                "failed": int(status.get("failed", 0) or 0),
                "skipped_existing": shard_skipped,
                "rate_per_second": status.get("rate_per_second", 0),
                "updated_at": status.get("updated_at"),
            }
        )
        done += shard_done + shard_skipped
        success += int(status.get("success", 0) or 0)
        failed += int(status.get("failed", 0) or 0)
        skipped_existing += shard_skipped
        pending += shard_pending
        total_items += int(status.get("total_items", 0) or 0)
        workers += int(status.get("workers", 0) or 0)
        rate += float(status.get("rate_per_second", 0) or 0)
        recent_failures.extend(status.get("recent_failures") or [])

    eligible = sum(spec["limit"] for spec in specs)
    eta = int(pending / rate) if rate > 0 and pending > 0 else 0 if pending == 0 else None
    return {
        "started_at": started_at,
        "updated_at": now_utc(),
        "all": False,
        "resume": True,
        "workers": workers or None,
        "eligible": eligible,
        "skipped_existing": skipped_existing,
        "pending": pending,
        "done": done,
        "success": success,
        "failed": failed,
        "total_items": total_items,
        "rate_per_second": round(rate, 3),
        "eta_seconds": eta,
        "recent_failures": recent_failures[-10:],
        "shards": shards,
    }


def merge_failures(specs: list[dict], output_path: Path) -> None:
    lines: list[str] = []
    for spec in specs:
        path = spec["failures_file"]
        if not path.exists():
            continue
        lines.extend(line for line in path.read_text().splitlines() if line.strip())
    output_path.write_text(("\n".join(lines) + "\n") if lines else "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    parser.add_argument("--status-file", default=str(DEFAULT_STATUS_FILE))
    parser.add_argument("--merged-failures-file", default=str(DEFAULT_MERGED_FAILURES_FILE))
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shard_dir = Path(args.shard_dir)
    status_file = Path(args.status_file)
    merged_failures_file = Path(args.merged_failures_file)
    specs = shard_specs(shard_dir)
    if not specs:
        print(f"no shard files found in {shard_dir}", flush=True)
        return 1

    started_at = now_utc()
    print(
        f"aggregating {len(specs)} gap shards from {shard_dir} into {status_file}",
        flush=True,
    )

    while True:
        payload = aggregate(specs, started_at)
        write_json(status_file, payload)
        merge_failures(specs, merged_failures_file)
        print(
            f"aggregate {payload['done']}/{payload['eligible']} | ok={payload['success']} "
            f"fail={payload['failed']} | {payload['rate_per_second']:.2f}/s | "
            f"eta={payload['eta_seconds']}s",
            flush=True,
        )
        if payload["eligible"] > 0 and payload["pending"] == 0 and payload["done"] >= payload["eligible"]:
            return 0
        time.sleep(max(args.poll_seconds, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
