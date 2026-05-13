#!/usr/bin/env python3
"""Wait for the targeted large-JSON retry lane, then rebuild slim site artifacts."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "data" / "manual_large_json_retry_status.json"
OUT_PATH = ROOT / "data" / "manual_large_json_post_slim_status.json"


def write_status(payload: dict) -> None:
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(OUT_PATH)


def read_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def run(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    write_status({"phase": "waiting_for_manual_retry"})
    while True:
        status = read_status()
        if not status:
            time.sleep(30)
            continue
        done = int(status.get("done") or 0)
        eligible = int(status.get("eligible") or 0)
        write_status({
            "phase": "waiting_for_manual_retry",
            "done": done,
            "eligible": eligible,
            "updated_at": status.get("updated_at"),
        })
        print(f"waiting for manual retry: {done}/{eligible}", flush=True)
        if eligible > 0 and done >= eligible:
            break
        time.sleep(30)

    write_status({"phase": "running_slim_parsed"})
    run([sys.executable, str(ROOT / "scripts" / "slim_parsed.py")])
    write_status({"phase": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
