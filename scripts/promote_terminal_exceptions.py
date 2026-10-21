#!/usr/bin/env python3
"""Auto-promote hard-failure CCNs from full_standardize_failures.jsonl into
data/coverage_terminal_exceptions.json.

Hard-failure error patterns:
  - exact "no_live_mrf"
  - starts with "seed:-:probe_html:" or "rediscovered:-:probe_html:"
  - starts with "seed:-:probe_reject:" or "rediscovered:-:probe_reject:" (same family — HTML wrapper)
  - starts with "timeout:" or contains ":timeout:" (e.g. "rediscovered:-:timeout:600s")

Idempotent: only appends CCNs not already in exceptions[]; preserves all existing entries.
Atomic write via .tmp + os.replace.
"""

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXC_PATH = ROOT / "data" / "coverage_terminal_exceptions.json"
FAIL_PATH = ROOT / "data" / "full_standardize_failures.jsonl"
DB_PATH = ROOT / "db" / "hospital_ledger.db"


def is_hard_failure(err: str) -> bool:
    if not err:
        return False
    if err == "no_live_mrf":
        return True
    if err.startswith("seed:-:probe_html:") or err.startswith("rediscovered:-:probe_html:"):
        return True
    if err.startswith("seed:-:probe_reject:") or err.startswith("rediscovered:-:probe_reject:"):
        return True
    if err.startswith("timeout:") or ":timeout:" in err:
        return True
    return False


def reason_for(err: str) -> str:
    if err == "no_live_mrf":
        return "no_live_mrf_after_rediscovery"
    if "probe_html" in err or "probe_reject" in err:
        return "mrf_url_returns_html_wrapper"
    if "timeout" in err:
        return "mrf_fetch_timeout"
    return "hard_failure"


def main() -> int:
    if not EXC_PATH.exists():
        print(f"ERROR: {EXC_PATH} not found", file=sys.stderr)
        return 1
    if not FAIL_PATH.exists():
        print(f"ERROR: {FAIL_PATH} not found", file=sys.stderr)
        return 1
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found", file=sys.stderr)
        return 1

    with open(EXC_PATH) as f:
        doc = json.load(f)
    exceptions = doc.get("exceptions", [])
    existing_ccns = {e["ccn"] for e in exceptions if "ccn" in e}
    existing_before = len(exceptions)

    # Read failures.jsonl — keep the LAST occurrence of each CCN (latest run wins)
    latest_fail = {}
    with open(FAIL_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ccn = row.get("ccn")
            if ccn:
                latest_fail[ccn] = row

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    added = 0
    skipped_present = 0
    skipped_non_hard = 0

    for ccn, row in latest_fail.items():
        err = row.get("error", "") or ""
        if not is_hard_failure(err):
            skipped_non_hard += 1
            continue
        if ccn in existing_ccns:
            skipped_present += 1
            continue

        # Hospital name
        h = cur.execute(
            "SELECT name FROM hospitals WHERE ccn = ?", (ccn,)
        ).fetchone()
        hospital_name = h["name"] if h else ccn

        # Original URL — first try mrf_seed, then mrf_rediscovered
        u = cur.execute(
            "SELECT mrf_url FROM mrf_seed WHERE ccn = ? AND mrf_url IS NOT NULL "
            "AND mrf_url != '' ORDER BY rowid LIMIT 1",
            (ccn,),
        ).fetchone()
        if u and u["mrf_url"]:
            original_url = u["mrf_url"]
        else:
            r = cur.execute(
                "SELECT candidate_url FROM mrf_rediscovered WHERE ccn = ? AND candidate_url "
                "IS NOT NULL AND candidate_url != '' ORDER BY rowid LIMIT 1",
                (ccn,),
            ).fetchone()
            original_url = r["candidate_url"] if r else ""

        # Probe history
        probes = cur.execute(
            "SELECT mrf_url, http_status, content_type, alive, probed_at "
            "FROM mrf_probe WHERE ccn = ? ORDER BY probed_at",
            (ccn,),
        ).fetchall()
        seed_probe_history = [
            {
                "mrf_url": p["mrf_url"] or "",
                "http_status": p["http_status"],
                "content_type": p["content_type"] or "",
                "alive": p["alive"],
                "probed_at": p["probed_at"] or "",
            }
            for p in probes
        ]

        entry = {
            "ccn": ccn,
            "hospital_name": hospital_name,
            "reason": reason_for(err),
            "provisional": True,
            "original_url": original_url,
            "failure_logs": ["data/full_standardize_failures.jsonl"],
            "seed_probe_history": seed_probe_history,
        }
        exceptions.append(entry)
        existing_ccns.add(ccn)
        added += 1

    conn.close()

    doc["exceptions"] = exceptions

    tmp_path = EXC_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    os.replace(tmp_path, EXC_PATH)

    print(
        f"existing={existing_before} -> final={len(exceptions)} | "
        f"added={added} | skipped_already_present={skipped_present} | "
        f"skipped_non_hard={skipped_non_hard}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
